# Python 版 Symphony tracker 运行时镜像。
#
# 选 node:22-bookworm 作为基础镜像而不是 python:3.12-slim：
#   - 需要 Node 跑 pi，bookworm 自带的 node 22 已满足，不必再从源码/二进制装
#   - bookworm 的 python3.11 满足项目要求的 Python >= 3.10
#   - 容器自带新 glibc，规避宿主机（如 CentOS 7）glibc 2.17 的兼容问题
FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/python

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-yaml ca-certificates procps xz-utils \
 && rm -rf /var/lib/apt/lists/*

# 与宿主机保持同一版本，避免 runtime 命令行为差异
RUN npm install -g @earendil-works/pi-coding-agent@1.5.2 \
 && pi --version

WORKDIR /app
COPY python/ /app/python/
COPY board/ /app/board/

RUN mkdir -p /data/tasks /data/workspaces /data/sessions /data/sessions-live

# api 与 board 共用同一镜像，靠 command 区分；默认跑调度器
CMD ["python3", "-m", "symphony", "--workflow", "/app/WORKFLOW.md"]
