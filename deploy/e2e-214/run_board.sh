#!/bin/bash
set -eu
export PYTHONPATH=/root/symphony-e2e/src/python
export PATH=/root/symphony-e2e/node22/bin:/usr/local/bin:/usr/bin:/bin
export HOME=/root
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PI_CODING_AGENT_DIR=/root/symphony-e2e/pi-agent
export SYMPHONY_WORKFLOW=/root/symphony-e2e/WORKFLOW.md
export SYMPHONY_STATE_URL=http://127.0.0.1:18320/api/v1/state
export SYMPHONY_SESSIONS=/root/symphony-e2e/sessions
export SYMPHONY_SESSIONS_LIVE=/root/symphony-e2e/sessions-live
export SYMPHONY_GITHUB_POLL_S=3
export PYTHONUNBUFFERED=1
exec /root/symphony-e2e/cpython312/bin/python3 /root/symphony-e2e/src/board/server.py --bind 0.0.0.0 --port 18322
