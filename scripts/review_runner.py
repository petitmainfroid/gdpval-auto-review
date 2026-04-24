#!/usr/bin/env python3
"""
MCPMark Playwright Auto-Review Runner (Daytona)

在 Daytona 沙箱中运行 claude -p 对单个 task 进行机审。

环境变量：
  必填：
    ZIP_URL      - TOS ZIP 文件 URL (tos://coding-rubrics/mcp/xxxx.zip)
  可选：
    RECORD_ID    - 记录 ID，用于 TOS 上传路径分组

输出：
    直接将模型审查结果 JSON 打印到 stdout
"""

import json
import os
import sys
import uuid
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _log(*args, **kwargs):
    """所有日志统一输出到 stderr，保持 stdout 只给结果 JSON。"""
    kwargs.setdefault("file", sys.stderr)
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)

# ---------------------------------------------------------------------------
# 本地仓库路径（script 在 scripts/ 下，rules/ 与 scripts/ 同级）
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).parent.resolve()
REPO_ROOT   = SCRIPT_DIR.parent
SCHEMA_PATH = REPO_ROOT / "rules" / "schema.json"
PROMPT_PATH = REPO_ROOT / "rules" / "prompt.md"

# ---------------------------------------------------------------------------
# TOS 配置
# ---------------------------------------------------------------------------
TOS_ACCESS_KEY_ID     = os.environ.get("TOS_ACCESS_KEY_ID", "")
TOS_ACCESS_KEY_SECRET = os.environ.get("TOS_ACCESS_KEY_SECRET", "")
TOS_REGION        = os.environ.get("TOS_REGION",    "cn-beijing")
TOS_ENDPOINT      = os.environ.get("TOS_ENDPOINT",  "tos-cn-beijing.volces.com")

# ---------------------------------------------------------------------------
# Daytona 配置
# ---------------------------------------------------------------------------
DAYTONA_API_KEY  = os.environ.get("DAYTONA_API_KEY", "")
DAYTONA_SNAPSHOT = os.environ.get("DAYTONA_SNAPSHOT", "claude-code-snapshot")

# ---------------------------------------------------------------------------
# OpenRouter 配置（claude-code-snapshot 使用 OpenRouter 代理）
# ---------------------------------------------------------------------------
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
CLAUDE_MODEL        = "google/gemini-3.1-pro-preview"

# ---------------------------------------------------------------------------
# 沙箱内路径
# ---------------------------------------------------------------------------
REMOTE_HOME   = "/home/daytona"
REMOTE_REVIEW = f"{REMOTE_HOME}/review"
REMOTE_SCHEMA = f"{REMOTE_REVIEW}/schema.json"
REMOTE_PROMPT = f"{REMOTE_REVIEW}/prompt.md"
REMOTE_TASK   = f"{REMOTE_REVIEW}/task"
REMOTE_OUTPUT = f"{REMOTE_REVIEW}/result_raw.json"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def sandbox_exec(sandbox, command: str, label: str = "cmd",
                 timeout: int = 300) -> tuple:
    """用 sandbox.process.exec() 执行命令，同步阻塞直到完成。
    timeout=0 表示无限等待（适合 claude 这类长时间任务）。
    返回 (exit_code: int, stdout: str)
    """
    _log(f"[INFO] [{label}] 执行: {command[:120]}{'...' if len(command)>120 else ''}")
    resp = sandbox.process.exec(command, timeout=timeout)
    _log(f"[INFO] [{label}] 完成 exit={resp.exit_code}")
    return resp.exit_code, resp.result or ""


def cleanup_sandbox(daytona, sandbox):
    """停止并删除沙箱（带超时保护）"""
    _log(f"[INFO] 清理 Sandbox: {sandbox.id}")

    def _do():
        try:
            sandbox.set_auto_delete_interval(0)
            sandbox.stop(timeout=30)
            _log("[INFO] Sandbox 已停止")
        except Exception as e:
            _log(f"[WARN] stop 失败: {e}，尝试 delete...")
            try:
                sandbox.delete(timeout=30)
            except Exception as e2:
                _log(f"[WARN] delete 也失败: {e2}")

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=90)
    if t.is_alive():
        _log("[WARN] 清理超时，依赖平台自动回收")


# ---------------------------------------------------------------------------
# 核心步骤
# ---------------------------------------------------------------------------

def setup_sandbox(sandbox, zip_url: str):
    """配置 tosutil、下载解压 ZIP、上传 rules 文件"""

    # 1. 配置 tosutil
    _log("[INFO] 配置 tosutil...")
    sandbox.process.exec(
        f"tosutil config -i {TOS_ACCESS_KEY_ID} -k {TOS_ACCESS_KEY_SECRET} "
        f"-e {TOS_ENDPOINT} -re {TOS_REGION}"
    )

    # 2. 下载 ZIP
    remote_zip = f"{REMOTE_REVIEW}/task.zip"
    sandbox.process.exec(f"mkdir -p {REMOTE_REVIEW}")
    _log(f"[INFO] 下载 ZIP: {zip_url}")
    code, out = sandbox_exec(sandbox,
        f"tosutil cp '{zip_url}' '{remote_zip}' 2>&1",
        label="download-zip", timeout=300)
    if code != 0:
        raise RuntimeError(f"ZIP 下载失败:\n{out[:500]}")

    # 3. 解压到 REMOTE_TASK
    sandbox.process.exec(f"mkdir -p '{REMOTE_TASK}'")
    sandbox.process.exec(f"unzip -o '{remote_zip}' -d '{REMOTE_TASK}'")
    r = sandbox.process.exec(f"ls '{REMOTE_TASK}'")
    _log(f"[INFO] task 目录内容: {r.result.strip()}")

    # 4. 上传 schema.json 和 prompt.md
    _log("[INFO] 上传 schema.json...")
    sandbox.fs.upload_file(SCHEMA_PATH.read_bytes(), REMOTE_SCHEMA)
    _log("[INFO] 上传 prompt.md...")
    sandbox.fs.upload_file(PROMPT_PATH.read_bytes(), REMOTE_PROMPT)

    # 5. 预创建 ~/.claude/settings.json
    #    避免 Claude Code 首次启动时触发交互式 login/初始化流程
    #    使用 ANTHROPIC_AUTH_TOKEN=<openrouter-key> 走 OpenRouter，无需 claude login
    _log("[INFO] 预创建 ~/.claude/settings.json ...")
    claude_settings = json.dumps({
        "enableToolSearch": False,
        "hasCompletedOnboarding": True,
        "permissions": {
            "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)"],
            "deny": []
        }
    }, indent=2)
    sandbox.process.exec("mkdir -p ~/.claude")
    sandbox.fs.upload_file(
        claude_settings.encode("utf-8"),
        "/root/.claude/settings.json"
    )

    _log("[INFO] 沙箱初始化完成")


def run_preflight(sandbox):
    """在运行 claude -p 之前做环境预检诊断"""
    _log("[DIAG] === 预检诊断 ===")

    checks = [
        ("当前用户",       "whoami"),
        ("claude 版本",    "claude --version 2>&1 || echo 'claude not found'"),
        ("环境变量",       "env | grep -iE 'ANTHROPIC|CLAUDE|IS_SANDBOX|API' | sort"),
        ("schema 文件",    f"ls -la '{REMOTE_SCHEMA}' 2>&1"),
        ("prompt 文件",    f"ls -la '{REMOTE_PROMPT}' 2>&1"),
        ("task 目录",      f"ls -la '{REMOTE_TASK}/' 2>&1"),
        ("schema 首行",    f"head -3 '{REMOTE_SCHEMA}' 2>&1"),
        ("磁盘空间",       "df -h / 2>&1 | tail -1"),
        # 检查 OpenRouter 可达性
        ("网络-openrouter","curl -m 5 -s -o /dev/null -w 'HTTP %{http_code} (%{time_total}s)' "
                          "https://openrouter.ai/api/v1/models 2>&1 || echo 'curl failed'"),
        # 关键：检查 api.anthropic.com 可达性（Claude Code 启动时会连接此地址做 auth 验证）
        ("网络-anthropic", "curl -m 5 -s -o /dev/null -w 'HTTP %{http_code} (%{time_total}s)' "
                          "https://api.anthropic.com/v1/models 2>&1 || echo 'unreachable'"),
        (".claude 目录",   "ls -la ~/.claude/ 2>&1 || echo '不存在'"),
        # 检查 /etc/hosts 有无干扰条目
        ("hosts-anthropic","grep anthropic /etc/hosts 2>&1 || echo '(无)'"),
    ]
    anthropic_reachable = True
    for name, cmd in checks:
        try:
            r = sandbox.process.exec(cmd)
            result_text = r.result.strip()
            _log(f"[DIAG] {name}: {result_text}")
            # 检测 api.anthropic.com 是否可达
            if name == "网络-anthropic":
                if "HTTP 200" not in result_text and "HTTP 4" not in result_text:
                    anthropic_reachable = False
        except Exception as e:
            _log(f"[DIAG] {name}: 执行失败 - {e}")

    # 若 api.anthropic.com 不可达（超时 / 防火墙拦截），Claude Code 启动时会永久挂起等待
    # 解决方案：将其绑定到 127.0.0.1，让连接立即被拒绝（快速失败）而非超时
    if not anthropic_reachable:
        _log("[DIAG] !! api.anthropic.com 不可达 — 已确认为挂起根因 !!")
        _log("[DIAG] 正在将 api.anthropic.com 绑定到 127.0.0.1 以避免无限挂起 ...")
        sandbox.process.exec(
            "grep -q 'api.anthropic.com' /etc/hosts || "
            "echo '127.0.0.1 api.anthropic.com' >> /etc/hosts"
        )
        sandbox.process.exec(
            "grep -q 'claude.ai' /etc/hosts || "
            "echo '127.0.0.1 claude.ai' >> /etc/hosts"
        )
        _log("[DIAG] hosts 已更新，Claude Code 认证失败将快速报错而非挂起")
    else:
        _log("[DIAG] api.anthropic.com 可达（正常）")

    _log("[DIAG] === 预检完成 ===")

    # -----------------------------------------------------------------------
    # 冒烟测试：exec() 直接拿 stdout，不涉及 session，无卡死风险
    # -----------------------------------------------------------------------
    _log("[DIAG] === 冒烟测试: claude -p 'echo test' ===")
    try:
        # 使用更简单的 echo 测试，避免 prompt 解析问题
        # 同时捕获 stderr 到 stdout 以便查看
        resp = sandbox.process.exec(
            "claude -p --dangerously-skip-permissions 'echo test'"
            " --output-format text 2>&1",
            timeout=60,
        )
        combined = resp.result or ""
        # 尝试分离 stdout 和 stderr（最后一个换行前的为 stdout）
        lines = combined.split("\n")
        stdout_lines = []
        stderr_lines = []
        for line in lines:
            if line.startswith("[Error]") or "error" in line.lower():
                stderr_lines.append(line)
            else:
                stdout_lines.append(line)
        stdout = "\n".join(stdout_lines).strip()
        _log(f"[DIAG] 冒烟测试 rc={resp.exit_code}  stdout={repr(stdout[:200])}")
        if stderr_lines:
            _log(f"[DIAG] 冒烟测试 stderr: {repr(stderr_lines[:5])}")
        if resp.exit_code != 0:
            _log(f"[WARN] 冒烟测试失败 (exit={resp.exit_code})，继续执行主任务")
        elif not stdout:
            _log("[WARN] 冒烟测试 rc=0 但输出为空，检查 stderr 是否有提示")
        else:
            _log("[DIAG] 冒烟测试通过")
    except Exception as e:
        _log(f"[WARN] 冒烟测试异常: {e}，跳过冒烟测试继续执行主任务")
    _log("[DIAG] === 冒烟测试完成 ===")


def run_claude(sandbox) -> str:
    """在沙箱内对 task 目录运行 claude -p。

    使用 sandbox.process.exec(timeout=0) 直接获取输出：
      - 同步阻塞，claude 完成后立即返回
      - stdout 就是 claude 的 JSON 输出，无需写文件再下载
      - exit_code 直接可用，无 session 卡死风险
    """
    run_preflight(sandbox)

    remote_stderr = f"{REMOTE_REVIEW}/claude_stderr.txt"
    # 使用 2>&1 将 stderr 合并到 stdout，确保 resp.result 包含所有输出
    cmd = (
        f"claude -p"
        f" --dangerously-skip-permissions"
        f" --json-schema \"$(cat '{REMOTE_SCHEMA}')\""
        f" --system-prompt-file '{REMOTE_PROMPT}'"
        f" 'Review the {REMOTE_TASK} directory."
        f" Output ONLY a bare JSON object matching the schema."
        f" No markdown, no explanation, no code fences.'"
        f" --output-format json"
        f" < /dev/null"
        f" 2>&1"                        # 合并 stderr 到 stdout
    )
    _log(f"[INFO] 启动 claude -p（exec 同步等待，无超时限制）...")
    resp = sandbox.process.exec(cmd, timeout=0)   # timeout=0 = 无限等待

    stdout = resp.result or ""
    if resp.exit_code != 0:
        raise RuntimeError(f"claude -p 失败 (exit={resp.exit_code}):\n{stdout[:500]}")
    return stdout


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams, DaytonaNotFoundError

    zip_url   = os.environ.get("ZIP_URL", "")
    record_id = os.environ.get("RECORD_ID", "")

    if not zip_url:
        _log("[ERROR] ZIP_URL 未设置")
        sys.exit(1)

    _log(f"=== MCPMark Playwright Auto-Review ===")
    _log(f"ZIP_URL  : {zip_url}")
    if record_id:
        _log(f"record_id: {record_id}")
    _log(f"\n[ENV] 关键环境变量:")
    _log(f"  DAYTONA_API_KEY     : {DAYTONA_API_KEY[:20]}... (长度: {len(DAYTONA_API_KEY)})")
    _log(f"  DAYTONA_SNAPSHOT    : {DAYTONA_SNAPSHOT}")
    _log(f"  OPENROUTER_BASE_URL : {OPENROUTER_BASE_URL}")
    _log(f"  OPENROUTER_API_KEY  : {OPENROUTER_API_KEY[:20]}... (长度: {len(OPENROUTER_API_KEY)})")
    _log(f"  CLAUDE_MODEL        : {CLAUDE_MODEL}")
    _log(f"  TOS_REGION          : {TOS_REGION}")
    _log(f"  TOS_ENDPOINT        : {TOS_ENDPOINT}")
    _log("=" * 44)

    daytona = Daytona(DaytonaConfig(api_key=DAYTONA_API_KEY))

    sandbox_name = f"playwright-review-{uuid.uuid4().hex[:6]}"

    # 清理同名旧沙箱
    try:
        existing = daytona.get(sandbox_name)
        _log(f"[INFO] 删除旧沙箱: {existing.id}")
        daytona.delete(existing)
    except DaytonaNotFoundError:
        pass

    _log(f"[INFO] 创建沙箱: {sandbox_name}")
    env_vars = {
        "ANTHROPIC_BASE_URL":              OPENROUTER_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN":             OPENROUTER_API_KEY,
        # 必须显式置空，防止 Claude Code 回落到 Anthropic 原生认证
        "ANTHROPIC_API_KEY":                "",
        "ANTHROPIC_DEFAULT_SONNET_MODEL":   CLAUDE_MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL":     CLAUDE_MODEL,
        "API_TIMEOUT_MS":                   "600000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "IS_SANDBOX": "1",
        # 禁用 Tool Search，避免与 OpenRouter 的 400 格式兼容问题（Issue #31380）
        "ENABLE_TOOL_SEARCH": "false",
    }
    _log(f"[ENV] 传入沙箱的环境变量:")
    for k, v in env_vars.items():
        if "KEY" in k or "TOKEN" in k:
            _log(f"  {k}: {v[:20] if v else '(空)'}... (长度: {len(v)})")
        else:
            _log(f"  {k}: {v}")
    
    sandbox = daytona.create(
        CreateSandboxFromSnapshotParams(
            name=sandbox_name,
            snapshot=DAYTONA_SNAPSHOT,
            network_block_all=False,
            auto_stop_interval=0,
            auto_delete_interval=0,
            env_vars=env_vars,
        ),
        timeout=0,
    )
    _log(f"[INFO] 沙箱创建成功: {sandbox.id}")

    try:
        setup_sandbox(sandbox, zip_url)
        stdout = run_claude(sandbox)

    except Exception as e:
        _log(f"[ERROR] 任务失败: {e}")
        print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)

    finally:
        cleanup_sandbox(daytona, sandbox)

    # -------------------------------------------------------------------------
    # stdout 是 claude -p --output-format json 的信封格式：
    #   { "type": "result", "result": "<模型回复文本>", "is_error": false, ... }
    # envelope["result"] 理想情况是裸 JSON，但模型有时会输出 Markdown。
    # 解析策略（优先级递减）：
    #   1. 直接 json.loads(inner_str)
    #   2. 从 ```json ... ``` 代码块中提取
    #   3. 找第一个 { 到最后一个 } 之间的文本尝试解析
    #   4. 全部失败则原样写入方便排查
    # -------------------------------------------------------------------------
    import re

    def _extract_review(inner_str: str) -> dict:
        # 策略1：直接解析
        try:
            return json.loads(inner_str)
        except json.JSONDecodeError:
            pass
        # 策略2：从 ```json ... ``` 块提取
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", inner_str)
        if m:
            return json.loads(m.group(1))
        # 策略3：找最外层 {...}
        start = inner_str.find("{")
        end   = inner_str.rfind("}")
        if start != -1 and end > start:
            return json.loads(inner_str[start:end + 1])
        raise ValueError("无法在模型输出中找到合法 JSON")

    try:
        envelope = json.loads(stdout)
        if envelope.get("is_error"):
            raise RuntimeError(f"claude 返回 is_error=true: {envelope.get('result','')[:300]}")

        # --json-schema 模式下，审查结果在 structured_output 字段（dict）；
        # 普通模式下在 result 字段（str）。优先取 structured_output。
        structured = envelope.get("structured_output")
        if isinstance(structured, dict) and structured:
            review = structured
            _log("[INFO] 从 structured_output 提取审查结果")
        else:
            inner_str = envelope.get("result", "")
            review = _extract_review(inner_str)
            _log("[INFO] 从 result 字段提取审查结果")

        result_text = json.dumps(review, ensure_ascii=False, indent=2)

        # review 应该符合 schema 格式（recommendation + reasons）
        if "recommendation" in review and "reasons" in review:
            _log("[INFO] review 符合 schema 格式")
            record = {
                "recommendation": review.get("recommendation", "revise"),
                "reasons": review.get("reasons", []),
            }
            if not record["reasons"]:
                record["reasons"] = ["机审结论: " + record["recommendation"]]
            record_text = json.dumps(record, ensure_ascii=False, indent=2)
        else:
            raise ValueError(f"review 不符合 schema 格式，缺少 recommendation 或 reasons 字段: {result_text[:200]}")
    except Exception as e:
        _log(f"[WARN] 解析 claude 输出失败({e})，原样输出")
        result_text = stdout
        record_text = json.dumps({"error": str(e)}, ensure_ascii=False, indent=2)

    _log(f"[RAW] claude 原始输出:\n{stdout}")
    _log(f"[RESULT] 完整审查结果:\n{result_text}")
    _log(f"[RECORD] 提取字段:\n{record_text}")
    print(record_text)


if __name__ == "__main__":
    main()
