# Hermes Fleet — 상위 폴더 다중 레포 하네스

[webrtc-app/hermes](../webrtc-app/hermes/README.md)의 Hermes(CrewAI) 가상 조직을 **상위 폴더(`/home/ubuntu`) 수준으로 올린 하네스**입니다.
root 아래 git 저장소 중 **프런트엔드(FE)와 백엔드(BE)가 모두 분명한 레포만** 골라, **새벽 01:00~08:00에 PC가 켜져 있을 때** 레포를 하나씩 순회하며
PM → 디자이너 → 개발자 ⇄ QA 크루가 티켓을 처리하고, 하루 한 번 검증을 통과한 브랜치만 기준 브랜치에 머지·push 합니다.

```
hermes-fleet/
├── fleet.yaml            # root, 작동 시간대, 머지 시각, 포함/제외 레포, 레포별 FE/BE 오버라이드
├── templates/org.yaml    # 공통 조직 정의(역할·도구·단계). 레포에 hermes/config/org.yaml 이 있으면 그것이 우선
├── hermes_fleet/
│   ├── repos.py          #   레포 탐지: FE/BE 디렉터리·스택(node|static|python|java)·테스트/빌드 명령
│   ├── fleet.py          #   데몬: 시간대 → 일일 머지 → 티켓 처리 → 발굴 (레포 라운드 로빈)
│   ├── pipeline.py       #   티켓 파이프라인 (QA FAIL → Dev 재작업 루프)
│   ├── discover.py       #   Tech Lead 제안(proposed)
│   ├── merge.py          #   QA PASS 브랜치 머지 → 프로필 검증 → push
│   ├── schedule.py       #   01:00-08:00 창, 종료 전 여유
│   ├── org.py / agents.py / prompts.py / backlog.py / llm.py / settings.py
│   └── tools/            #   레포 루트로 샌드박싱된 fs / shell(허용 목록) / git
├── scripts/hermes-fleet          # CLI 래퍼 (uv)
├── scripts/install-cron.sh       # 01:00~07:59 10분마다 기동 시도(flock) — WSL 기본
├── scripts/install-systemd.sh    # systemd 가 켜진 환경용(선택)
├── state/   (gitignore)  # fleet.json(커서), <repo>.json(처리 기록·머지 기록), PAUSE, daemon.lock
├── logs/    (gitignore)  # daemon.log
└── tests/                # pytest (LLM 불필요)
```

## 작업 대상 선정 — "FE, BE 가 분명한 레포"

`hermes-fleet repos --all` 로 확인합니다. 조건:

| 사이드 | 탐지 규칙 | 스택 → 검증 명령 |
|---|---|---|
| FE | `frontend/ fe/ client/ web/ ui/` 또는 이름에 `front` 포함, 또는 루트 `package.json`에 vite/react/vue 등 | node: `npm test` / `npm run build` (package.json scripts 기준) · static: 명령 없음 |
| BE | `backend/ server/ api/ signaling/ app/` 또는 이름에 `back`/`server` 포함, 또는 루트 `pom.xml`+`src/main` | python: `uv run --with pytest --with-requirements … python -m pytest` · java: `mvn -q -B test` · node: `npm test` |

둘 다 있어야 대상이며, git 저장소가 아니거나 `exclude` 에 있으면 제외됩니다. 자동 탐지가 틀리면 `fleet.yaml` 의 `repos.<name>.frontend/backend` 로 지정합니다.

## 하루 흐름 (cron 기준)

| 시각 | 동작 |
|---|---|
| 01:00~ | cron 이 10분마다 기동 시도 → 이미 돌고 있으면 flock 으로 무시. PC가 03:00에 켜지면 03:10 안에 시작 |
| 01:00~07:15 | 커서 다음 레포부터 `todo` 티켓이 있는 레포를 찾아 `hermes/<ID>` 브랜치에서 PM→Design→Dev⇄QA 실행, **브랜치에 커밋**(push 없음). QA PASS → `review`, FAIL/오류 → `blocked`. todo 가 모두 비면 Tech Lead 가 레포 하나를 분석해 `proposed` 티켓을 커밋 |
| 07:15~ | 새 작업 시작 안 함(`no_new_work_minutes_before_end: 45`). 진행 중 티켓은 마침 |
| 07:30 | 레포마다 하루 한 번 **자동 머지**: `git pull --rebase` → `review` 브랜치 `--no-ff` 머지 → 레포 프로필 테스트·빌드 실행 → 통과 시 티켓 `done`, `origin` push, 브랜치 삭제. 충돌/검증 실패는 되돌리고 `review` 로 남김 |
| 08:00 | 시간대 종료 → 데몬 종료. cron 은 08시대부터 기동하지 않음 |

사람이 하는 일은 webrtc-app 과 같습니다: 제안 승인(`approve`), 자동 머지 전 리뷰(막으려면 티켓을 `blocked` 로), blocked 티켓 처리.
레포가 기준 브랜치가 아닌 다른 브랜치에 있거나 작업 트리가 더러우면 **건드리지 않고 건너뜁니다**(사람이 작업 중인 것으로 간주).

## 설치

```bash
cd /home/ubuntu/hermes-fleet
cp -n .env.example .env            # Ollama 가 Windows 호스트면 수정 불필요(게이트웨이 자동 탐지)
uv sync --extra dev
uv run pytest                      # 자체 테스트
scripts/hermes-fleet check         # 엔드포인트·모델(hermes3:8b, qwen2.5-coder:7b)·설정·대상 레포 점검
scripts/hermes-fleet repos -v      # 대상 레포와 검증 명령
scripts/install-cron.sh            # 새벽 자동 실행 등록 (해제: --remove)
```

## 명령

```bash
scripts/hermes-fleet repos [--all] [-v]            # 대상 레포 / 제외 사유
scripts/hermes-fleet status                        # 레포별 승인 대기·리뷰 대기·막힘·todo
scripts/hermes-fleet backlog [repo] [--status s]
scripts/hermes-fleet approve <repo> HX-001 [--commit]
scripts/hermes-fleet new <repo> HX-010 "제목"
scripts/hermes-fleet run <repo> HX-010 [--stages dev,qa] [--commit] [--no-branch]
scripts/hermes-fleet discover <repo> [--max 2]
scripts/hermes-fleet merge [repo] [--dry-run] [--no-push]
scripts/hermes-fleet daemon [--once] [--forever] [--only repo ...] [--active-hours ""] [--no-discover]
scripts/hermes-fleet pause / resume
scripts/hermes-fleet init [repo ...] [--commit]    # hermes/backlog 스캐폴드(데몬이 첫 작업 때 자동 생성하기도 함)
```

`daemon` 은 기본(cron 모드)에서 시간대 밖이면 종료합니다. systemd 로 상시 실행하려면 `--forever`(유닛에 포함).

## 레포 안의 규약 (webrtc-app 과 동일)

- 티켓: `<repo>/hermes/backlog/HX-nnn-*.md` (YAML frontmatter + 본문). `area` 는 frontend|backend|signaling|fullstack|api|db|infra|docs.
- 산출물: `<repo>/hermes/workspace/<ID>/` (prd.md, design.md, dev-report.md, qa-report.md, summary.md). 로그·발굴 원문은 gitignore.
- 조직 오버라이드: `<repo>/hermes/config/org.yaml` (webrtc-app 은 자체 정의 사용).
- 브랜치 `hermes/<ID>`, 커밋 서명 `Co-Authored-By: Hermes Fleet`.

## 안전장치

- 에이전트 도구는 레포 루트로 샌드박싱: `.env` `.git` `node_modules` `dist` `target` 접근 불가, 셸은 허용 목록(npm/npx/node, pytest/uv run, mvn, 읽기 전용 git/ls/cat/grep)만, `rm sudo curl docker aws pip` 등 금지.
- push 는 오직 일일 머지 단계에서, 검증 통과분만. `auto_merge.require_verification: true` 면 실행 가능한 테스트/빌드가 없는 레포는 자동 머지하지 않습니다(`repos -v` 의 사유에 표시).
- 전체 중지: `scripts/hermes-fleet pause` 또는 `scripts/install-cron.sh --remove`. 특정 레포 제외: `fleet.yaml` `exclude`.

## 한계

- 8B 모델 특성상 긴 파일 재작성에서 실수할 수 있습니다. 티켓을 작게 유지하고 수용 기준을 명확히 쓰는 편이 효과적입니다.
- Java(Spring) 레포는 `mvn test` 가 의존성을 내려받아 첫 실행이 느립니다. 정적 HTML 프런트엔드는 테스트가 없어 BE 검증에만 의존합니다.
- 추론은 Windows Ollama 에서 일어나므로 GPU 메모리를 비워 두면 빠릅니다. 동시 처리는 하지 않고 한 번에 한 레포·한 티켓만 처리합니다.
