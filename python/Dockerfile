# Symphony 调度器（Python 版）运行时镜像。
# 目标宿主 192.168.1.199：容器内同时运行 orchestrator 与 pi agent，
# 需 --network host 运行（git-ssh.sh / 代理均指向宿主回环 127.0.0.1:7890）。
FROM node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-yaml \
        git openssh-client netcat-openbsd bash curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 与宿主机一致的 pi 版本（宿主 node 24 + pi 1.3.0）。
RUN npm install -g @earendil-works/pi-coding-agent@1.3.0 \
    && npm cache clean --force

WORKDIR /app
COPY symphony ./symphony

# WORKFLOW.md 与所有状态目录以宿主同路径 bind-mount 进容器。
ENTRYPOINT ["python3", "-m", "symphony", "--workflow", "/root/symphony/config/WORKFLOW.md"]
