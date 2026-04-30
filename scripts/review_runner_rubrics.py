#!/usr/bin/env python3
"""
MCPMark Playwright Auto-Review Runner (Daytona) — Rubrics 机审

在 Daytona 沙箱中运行 claude -p，使用 prompt_rubrics.md 作为系统提示词，
对 /workspace/rubrics.json 进行机审，结果输出到 /workspace/result.json。

环境变量：
  可选：
    RECORD_ID    - 记录 ID
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
    kwargs.setdefault("file", sys.stderr)
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)

# ---------------------------------------------------------------------------
# 本地路径
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).parent.resolve()
REPO_ROOT   = SCRIPT_DIR.parent

# 审查标准（系统提示词）
LOCAL_PROMPT  = REPO_ROOT / "rules" / "prompt_rubrics.md"
# 输出格式约束
LOCAL_SCHEMA  = REPO_ROOT / "rules" / "schema.json"

# 被审查的文件（从飞书拉取到本地的 rubrics）
LOCAL_RUBRICS = Path("/workspace/rubrics.json")
# 机审结果输出
LOCAL_RESULT  = Path("/workspace/result.json")

# ---------------------------------------------------------------------------
# Daytona / OpenRouter 配置
# ---------------------------------------------------------------------------
DAYTONA_API_KEY  = os.environ.get("DAYTONA_API_KEY", "")
DAYTONA_SNAPSHOT = os.environ.get("DAYTONA_SNAPSHOT", "claude-code-snapshot")

OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
CLAUDE_MODEL        = "google/gemini-3.1-pro-preview"

# ---------------------------------------------------------------------------
# 沙箱内路径
# ---------------------------------------------------------------------------
REMOTE_HOME    = "/home/daytona"
REMOTE_REVIEW  = f"{REMOTE_HOME}/review"
REMOTE_RUBRICS = f"{REMOTE_REVIEW}/rubrics.json"   # 被审查内容
REMOTE_PROMPT  = f"{REMOTE_REVIEW}/prompt.md"      # 系统提示词（实际为 rubrics 标准）
REMOTE_SCHEMA  = f"{REMOTE_REVIEW}/schema.json"    # JSON 输出格式约束


def sandbox_exec(sandbox, command: str, label: str = "cmd",
                 timeout: int = 300) -> tuple:
    _log(f"[INFO] [{label}] 执行: {command[:120]}{'...' if len(command)>120 else ''}")
    resp = sandbox.process.exec(command, timeout=timeout)
    _log(f"[INFO] [{label}] 完成 exit={resp.exit_code}")
    return resp.exit_code, resp.result or ""


def cleanup_sandbox(daytona, sandbox):
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


def setup_sandbox(sandbox):
    """上传 rubrics.json、schema.json 和 prompt_rubrics.md 到沙箱"""

    # 1. 检查本地文件
    if not LOCAL_RUBRICS.exists():
        raise FileNotFoundError(f"本地 rubrics.json 不存在: {LOCAL_RUBRICS}")
    if not LOCAL_PROMPT.exists():
        raise FileNotFoundError(f"本地 prompt_rubrics.md 不存在: {LOCAL_PROMPT}")
    if not LOCAL_SCHEMA.exists():
        raise FileNotFoundError(f"本地 schema.json 不存在: {LOCAL_SCHEMA}")

    # 2. 创建沙箱工作目录
    sandbox.process.exec(f"mkdir -p {REMOTE_REVIEW}")

    # 3. 上传 rubrics.json（被审查内容）
    _log(f"[INFO] 上传 rubrics.json ({LOCAL_RUBRICS.stat().st_size} bytes)...")
    sandbox.fs.upload_file(LOCAL_RUBRICS.read_bytes(), REMOTE_RUBRICS)

    # 4. 上传 schema.json（输出格式约束）
    _log("[INFO] 上传 schema.json...")
    sandbox.fs.upload_file(LOCAL_SCHEMA.read_bytes(), REMOTE_SCHEMA)

    # 5. 上传 prompt_rubrics.md（沙箱内命名为 prompt.md，作为系统提示词）
    _log("[INFO] 上传 prompt_rubrics.md（沙箱内作为 prompt.md）...")
    sandbox.fs.upload_file(LOCAL_PROMPT.read_bytes(), REMOTE_PROMPT)

    # 6. 预创建 ~/.claude/settings.json
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
    """预检诊断"""
    _log("[DIAG] === 预检诊断 ===")

    checks = [
        ("当前用户",       "whoami"),
        ("claude 版本",    "claude --version 2>&1 || echo 'claude not found'"),
        ("环境变量",       "env | grep -iE 'ANTHROPIC|CLAUDE|IS_SANDBOX|API' | sort"),
        ("schema 文件",    f"ls -la '{REMOTE_SCHEMA}' 2>&1"),
        ("prompt 文件",    f"ls -la '{REMOTE_PROMPT}' 2>&1"),
        ("rubrics 文件",   f"ls -la '{REMOTE_RUBRICS}' 2>&1"),
        ("rubrics 首行",   f"head -3 '{REMOTE_RUBRICS}' 2>&1"),
        ("schema 首行",    f"head -3 '{REMOTE_SCHEMA}' 2>&1"),
        ("磁盘空间",       "df -h / 2>&1 | tail -1"),
        ("网络-openrouter","curl -m 5 -s -o /dev/null -w 'HTTP %{http_code} (%{time_total}s)' "
                          "https://openrouter.ai/api/v1/models 2>&1 || echo 'curl failed'"),
        ("网络-anthropic", "curl -m 5 -s -o /dev/null -w 'HTTP %{http_code} (%{time_total}s)' "
                          "https://api.anthropic.com/v1/models 2>&1 || echo 'unreachable'"),
        (".claude 目录",   "ls -la ~/.claude/ 2>&1 || echo '不存在'"),
        ("hosts-anthropic","grep anthropic /etc/hosts 2>&1 || echo '(无)'"),
    ]
    anthropic_reachable = True
    for name, cmd in checks:
        try:
            r = sandbox.process.exec(cmd)
            result_text = r.result.strip()
            _log(f"[DIAG] {name}: {result_text}")
            if name == "网络-anthropic":
                if "HTTP 200" not in result_text and "HTTP 4" not in result_text:
                    anthropic_reachable = False
        except Exception as e:
            _log(f"[DIAG] {name}: 执行失败 - {e}")

    if not anthropic_reachable:
        _log("[DIAG] !! api.anthropic.com 不可达 — 已确认为挂起根因 !!")
        _log("[DIAG] 正在将 api.anthropic.com 绑定到 127.0.0.1 ...")
        sandbox.process.exec(
            "grep -q 'api.anthropic.com' /etc/hosts || "
            "echo '127.0.0.1 api.anthropic.com' >> /etc/hosts"
        )
        sandbox.process.exec(
            "grep -q 'claude.ai' /etc/hosts || "
            "echo '127.0.0.1 claude.ai' >> /etc/hosts"
        )
        _log("[DIAG] hosts 已更新")
    else:
        _log("[DIAG] api.anthropic.com 可达（正常）")

    _log("[DIAG] === 预检完成 ===")

    _log("[DIAG] === 冒烟测试: claude -p 'echo test' ===")
    try:
        resp = sandbox.process.exec(
            "claude -p --dangerously-skip-permissions 'echo test'"
            " --output-format text 2>&1",
            timeout=60,
        )
        combined = resp.result or ""
        lines = combined.split("\n")
        stdout_lines = []
        stderr_lines = []
        for line in lines:
            if line.startswith("[Error]") or "error" in line.lower():
                stderr_lines.append(line)
            else:
                stdout_lines.append(line)
        stdout_text = "\n".join(stdout_lines).strip()
        _log(f"[DIAG] 冒烟测试 rc={resp.exit_code}  stdout={repr(stdout_text[:200])}")
        if stderr_lines:
            _log(f"[DIAG] 冒烟测试 stderr: {repr(stderr_lines[:5])}")
        if resp.exit_code != 0:
            _log(f"[WARN] 冒烟测试失败 (exit={resp.exit_code})，继续执行主任务")
        elif not stdout_text:
            _log("[WARN] 冒烟测试 rc=0 但输出为空")
        else:
            _log("[DIAG] 冒烟测试通过")
    except Exception as e:
        _log(f"[WARN] 冒烟测试异常: {e}，跳过继续执行主任务")
    _log("[DIAG] === 冒烟测试完成 ===")


def run_claude(sandbox) -> str:
    """运行 claude -p，使用 prompt_rubrics.md 作为系统提示词审查 rubrics.json"""
    run_preflight(sandbox)

    # 系统提示词 = rubrics 评分标准（REMOTE_PROMPT 实际内容是 prompt_rubrics.md）
    # User Prompt = 要求 Claude 读取 rubrics.json 并根据标准审查
    cmd = (
        f"claude -p"
        f" --dangerously-skip-permissions"
        f" --json-schema \"$(cat '{REMOTE_SCHEMA}')\""
        f" --system-prompt-file '{REMOTE_PROMPT}'"
        f" 'Read and review the content of {REMOTE_RUBRICS} using the rubrics defined in the system prompt."
        f" Evaluate each criterion carefully and output ONLY a bare JSON object matching the schema."
        f" No markdown, no explanation, no code fences.'"
        f" --output-format json"
        f" < /dev/null"
        f" 2>&1"
    )
    _log(f"[INFO] 启动 claude -p（exec 同步等待，无超时限制）...")
    resp = sandbox.process.exec(cmd, timeout=0)

    stdout = resp.result or ""
    if resp.exit_code != 0:
        raise RuntimeError(f"claude -p 失败 (exit={resp.exit_code}):\n{stdout[:500]}")
    return stdout


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams, DaytonaNotFoundError

    record_id = os.environ.get("RECORD_ID", "")

    _log(f"=== MCPMark Rubrics Auto-Review ===")
    _log(f"输入文件 : {LOCAL_RUBRICS}")
    _log(f"输出文件 : {LOCAL_RESULT}")
    if record_id:
        _log(f"record_id: {record_id}")
    _log(f"\n[ENV] 关键环境变量:")
    _log(f"  DAYTONA_API_KEY     : {DAYTONA_API_KEY[:20]}... (长度: {len(DAYTONA_API_KEY)})")
    _log(f"  DAYTONA_SNAPSHOT    : {DAYTONA_SNAPSHOT}")
    _log(f"  OPENROUTER_BASE_URL : {OPENROUTER_BASE_URL}")
    _log(f"  OPENROUTER_API_KEY  : {OPENROUTER_API_KEY[:20]}... (长度: {len(OPENROUTER_API_KEY)})")
    _log(f"  CLAUDE_MODEL        : {CLAUDE_MODEL}")
    _log("=" * 44)

    daytona = Daytona(DaytonaConfig(api_key=DAYTONA_API_KEY))

    sandbox_name = f"rubrics-review-{uuid.uuid4().hex[:6]}"

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
        "ANTHROPIC_API_KEY":                "",
        "ANTHROPIC_DEFAULT_SONNET_MODEL":   CLAUDE_MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL":     CLAUDE_MODEL,
        "API_TIMEOUT_MS":                   "600000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "IS_SANDBOX": "1",
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
        setup_sandbox(sandbox)
        stdout = run_claude(sandbox)

    except Exception as e:
        _log(f"[ERROR] 任务失败: {e}")
        # 即使失败也写入错误信息到 result.json
        error_record = {"error": str(e)}
        LOCAL_RESULT.write_text(
            json.dumps(error_record, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        _log(f"[INFO] 错误信息已保存到: {LOCAL_RESULT}")
        print(json.dumps(error_record, ensure_ascii=False, indent=2))
        sys.exit(1)

    finally:
        cleanup_sandbox(daytona, sandbox)

    import re

    def _extract_review(inner_str: str) -> dict:
        try:
            return json.loads(inner_str)
        except json.JSONDecodeError:
            pass
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", inner_str)
        if m:
            return json.loads(m.group(1))
        start = inner_str.find("{")
        end   = inner_str.rfind("}")
        if start != -1 and end > start:
            return json.loads(inner_str[start:end + 1])
        raise ValueError("无法在模型输出中找到合法 JSON")

    try:
        envelope = json.loads(stdout)
        if envelope.get("is_error"):
            raise RuntimeError(f"claude 返回 is_error=true: {envelope.get('result','')[:300]}")

        structured = envelope.get("structured_output")
        if isinstance(structured, dict) and structured:
            review = structured
            _log("[INFO] 从 structured_output 提取审查结果")
        else:
            inner_str = envelope.get("result", "")
            review = _extract_review(inner_str)
            _log("[INFO] 从 result 字段提取审查结果")

        result_text = json.dumps(review, ensure_ascii=False, indent=2)

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
        record_text = json.dumps({"error": str(e), "raw": stdout}, ensure_ascii=False, indent=2)

    # 写入 /workspace/result.json
    LOCAL_RESULT.write_text(record_text, encoding="utf-8")
    _log(f"[INFO] 机审结果已保存到: {LOCAL_RESULT}")

    _log(f"[RAW] claude 原始输出:\n{stdout}")
    _log(f"[RESULT] 完整审查结果:\n{result_text}")
    _log(f"[RECORD] 提取字段:\n{record_text}")
    print(record_text)


if __name__ == "__main__":
    main()
