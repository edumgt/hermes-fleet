#!/usr/bin/env bash
# 공개(public) 전 점검: 추적 중인 파일과 git 히스토리에서 개인 환경 정보가 보이는지 찾는다.
#   - 홈 경로(/home/<user>), 형제 레포 이름(상위 디렉터리의 git 저장소들), 로컬 fleet.yaml 의 include/exclude/repos/approval 이름과 배포 메모의 도메인
#   - 클라우드 식별자(CloudFront E…, arn:aws, AWS 액세스 키)
# 사용:
#   scripts/publish-check.sh [추가패턴 ...]          현재 브랜치 전체 히스토리 + 추적 파일 점검. 걸리면 종료 코드 1
#   scripts/publish-check.sh --orphan public          현재 추적 파일 상태만 담은 단일 커밋의 orphan 브랜치 'public' 을 만들고(기존 브랜치·HEAD 는 건드리지 않음) 그 브랜치를 점검
#                                                     → 통과하면 `git push <public-remote> public:main` 으로 공개한다 (히스토리에 남은 개인 정보가 따라가지 않는다)
set -uo pipefail
FLEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FLEET_DIR"
SELF="$(basename "$FLEET_DIR")"
ORPHAN=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --orphan) ORPHAN="${2:-public}"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

PAT=()
add() { local p="$1"; [ -z "$p" ] && return; [ "${#p}" -lt 4 ] && return; [ "$p" = "$SELF" ] && return; for q in "${PAT[@]:-}"; do [ "$q" = "$p" ] && return; done; PAT+=("$p"); }
add "/home/$(id -un)"
case "${HOME:-}" in /root|/|"") ;; *) add "$HOME" ;; esac       # /root 는 uv 기본 경로(/root/.local/bin)와 겹쳐 제외
for d in "$(dirname "$FLEET_DIR")"/*/; do n="$(basename "$d")"; [ -d "$d/.git" ] && add "$n"; done
if [ -f fleet.yaml ] && command -v python3 >/dev/null; then
  while IFS= read -r n; do add "$n"; done < <(python3 - <<'EOF'
import yaml, sys
try:
    d = yaml.safe_load(open("fleet.yaml", encoding="utf-8")) or {}
except Exception:
    sys.exit(0)
names = set(map(str, d.get("include") or [])) | set(map(str, d.get("exclude") or [])) | set(map(str, (d.get("repos") or {}).keys())) | set(map(str, ((d.get("approval") or {}).get("repos") or {}).keys()))
for n in sorted(names): print(n)
for r in (d.get("repos") or {}).values():
    if isinstance(r, dict):
        dep = r.get("deploy") or {}
        for tok in str(dep.get("note", "") + " " + str(dep.get("target", "")) + " " + str(dep.get("command", ""))).replace(",", " ").split():
            if "." in tok and "/" not in tok and len(tok) > 5: print(tok.strip("()"))
EOF
)
fi
for p in "${ARGS[@]:-}"; do add "$p"; done
REGEX='E[A-Z0-9]{13}\b|arn:aws|AKIA[0-9A-Z]{16}'
for p in "${PAT[@]:-}"; do [ -n "$p" ] && REGEX="$REGEX|$(printf '%s' "$p" | sed 's/[.[\*^$()+?{}|\\/]/\\&/g')"; done
EXCL=(':!qa/out' ':!scripts/publish-check.sh' ':!*.lock' ':!uv.lock')

if [ -n "$ORPHAN" ]; then
  TMP_INDEX="$(mktemp)"; rm -f "$TMP_INDEX"
  GIT_INDEX_FILE="$TMP_INDEX" git read-tree --empty
  GIT_INDEX_FILE="$TMP_INDEX" git add -A -- . ':!qa/out'
  TREE="$(GIT_INDEX_FILE="$TMP_INDEX" git write-tree)"; rm -f "$TMP_INDEX"
  COMMIT="$(git commit-tree "$TREE" -m "Hermes Fleet — public release (history squashed)")"
  git branch -f "$ORPHAN" "$COMMIT"
  echo "orphan 브랜치 '$ORPHAN' 생성: $(git rev-parse --short "$COMMIT") (현재 추적 파일 + 새 파일, .gitignore 적용, 커밋 1개)"
  REV="$ORPHAN"; FILES_REV="$ORPHAN"
else
  REV="--all"; FILES_REV=""
fi

echo "검사 패턴 ${#PAT[@]}개 + 클라우드 식별자"
echo "== 추적 파일${FILES_REV:+ (브랜치 $FILES_REV)}"
hits=0
if [ -n "$FILES_REV" ]; then
  out="$(git grep -n -I -E "$REGEX" "$FILES_REV" -- . "${EXCL[@]}" 2>/dev/null)"
else
  out="$(git grep -n -I -E "$REGEX" -- . "${EXCL[@]}" 2>/dev/null)"
fi
if [ -n "$out" ]; then echo "$out" | head -60; hits=1; else echo "   없음"; fi
echo "== 커밋 히스토리 (추가/삭제 줄)"
hout="$(git log $REV -p --no-color -- . ':!uv.lock' 2>/dev/null | grep -n -E "^[+-].*($REGEX)" | head -40)"
if [ -n "$hout" ]; then echo "$hout"; hits=1; else echo "   없음"; fi
echo "== 커밋 메시지"
mout="$(git log $REV --format='%h %s%n%b' | grep -n -E "$REGEX" | head -20)"
if [ -n "$mout" ]; then echo "$mout"; hits=1; else echo "   없음"; fi
if [ "$hits" = 1 ]; then
  echo
  echo "✗ 개인 환경 정보가 남아 있습니다. 파일은 수정하고, 히스토리는 '$0 --orphan public' 으로 단일 커밋 브랜치를 만들어 그것만 공개하세요 (또는 git filter-repo)."
  exit 1
fi
echo "✓ 공개 가능: 개인 환경 정보가 보이지 않습니다.${ORPHAN:+  → git push <public-remote> $ORPHAN:main}"
