FROM elixir:1.19-otp-28

ARG NODE_VERSION=22.22.0
ARG HTTP_PROXY_URL=

# 214 无法直连 deb.debian.org / repo.hex.pm / builds.hex.pm，故：
#   apt -> 阿里云；hex 安装与依赖 -> 199 代理 + upyun 镜像
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
      if [ -f "$f" ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' "$f"; \
      fi; \
    done; \
    apt-get update; \
    apt-get install -y --no-install-recommends git openssh-client ca-certificates curl xz-utils procps; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm -f /tmp/node.tar.xz; \
    node --version; npm --version

RUN set -eux; \
    if [ -n "$HTTP_PROXY_URL" ]; then \
      export HTTP_PROXY="$HTTP_PROXY_URL" HTTPS_PROXY="$HTTP_PROXY_URL"; \
    fi; \
    npm install -g @openai/codex; \
    codex --version

ENV MIX_ENV=prod

RUN set -eux; \
    if [ -n "$HTTP_PROXY_URL" ]; then \
      export HTTP_PROXY="$HTTP_PROXY_URL" HTTPS_PROXY="$HTTP_PROXY_URL"; \
    fi; \
    mix local.hex --force

RUN git config --system user.name "Symphony Agent" \
 && git config --system user.email "symphony@localhost" \
 && git config --system --add safe.directory '*'

WORKDIR /app
COPY elixir/ /app/

RUN set -eux; \
    if [ -n "$HTTP_PROXY_URL" ]; then \
      export HTTP_PROXY="$HTTP_PROXY_URL" HTTPS_PROXY="$HTTP_PROXY_URL"; \
    fi; \
    mix deps.get; \
    mix escript.build

RUN mkdir -p /workspaces /config /logs

ENTRYPOINT ["/app/bin/symphony"]
CMD ["/config/WORKFLOW.md", "--logs-root", "/logs", "--port", "8317", \
     "--i-understand-that-this-will-be-running-without-the-usual-guardrails"]
