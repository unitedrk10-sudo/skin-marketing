#!/usr/bin/env bash
# 예전 설치 명령 호환용 — 실제 설치는 hermes/install.py (Windows·macOS·Linux 공통, 옵션·환경변수도 같다)
#   bash hermes/install.sh
set -euo pipefail
PY="${PYTHON:-$(command -v python3 || command -v python)}"
exec "$PY" "$(dirname "$0")/install.py" "$@"
