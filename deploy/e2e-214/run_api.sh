#!/bin/bash
set -eu
export PYTHONPATH=/root/symphony-e2e/src/python
export PATH=/root/symphony-e2e/node22/bin:/usr/local/bin:/usr/bin:/bin
export HOME=/root
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PI_CODING_AGENT_DIR=/root/symphony-e2e/pi-agent
export GITHUB_TOKEN="$(gh auth token)"
exec /root/symphony-e2e/cpython312/bin/python3 -m symphony --workflow /root/symphony-e2e/WORKFLOW.md
