FROM python:3.11-slim

WORKDIR /app

# 安装系统工具（git/curl/unzip 是 review_runner.py 里调用的基础命令）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    unzip \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# 升级 pip 到最新版，避免解析依赖出错
RUN pip install --no-cache-dir --upgrade pip

# 安装 Python 依赖（daytona-sdk 在火山引擎镜像可能没有，用官方 PyPI 兜底）
# 修改点：ivolces → volces（公网可用）
RUN pip install --no-cache-dir \
    -i https://mirrors.volces.com/pypi/simple/  \
    --extra-index-url https://pypi.org/simple/  \
    daytona-sdk \
    python-dotenv

# 把你的代码复制进镜像
COPY scripts /app/scripts
COPY rules /app/rules

# 环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# 流水线拉下来的代码会挂载到 /workspace，但脚本本身在 /app
WORKDIR /workspace
