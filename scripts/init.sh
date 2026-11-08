#!/usr/bin/env bash
# 받은 환경 초기화: 의존성 설치 → 형제 레포 스캔 → fleet.yaml/.env 생성 → LLM 점검 → 다음 단계 안내
#   scripts/init.sh                 (대화형: 대상 레포 선택)
#   scripts/init.sh --yes           (비대화: FE+BE 가 탐지된 레포 전부 선택)
#   scripts/init.sh --root /path/to/repos --provider openai --base-url http://localhost:1234/v1 --model qwen2.5-coder:7b
set -euo pipefail
FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
cd "$FLEET_DIR"
command -v uv >/dev/null || { echo "uv 가 필요합니다: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }
uv sync --extra dev -q
exec uv run hermes-fleet init "$@"
