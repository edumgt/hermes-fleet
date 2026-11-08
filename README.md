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
│   ├── fleetfile.py      #   fleet.yaml include/exclude 편집(웹 모니터의 레포 선택/해제)
│   ├── org.py / agents.py / prompts.py / backlog.py / llm.py / settings.py
│   ├── tools/            #   레포 루트로 샌드박싱된 fs / shell(허용 목록) / git
│   └── monitor/          #   웹 모니터(읽기 전용): collect.py(수집) · app.py(FastAPI) · static/(SPA)
├── docker-compose.yml / docker/monitor.Dockerfile   # 웹 모니터 컨테이너
├── scripts/hermes-fleet          # CLI 래퍼 (uv)
├── scripts/monitor.sh            # 웹 모니터 up/down/logs/status/local/demo
├── scripts/demo_fleet.py         # 미리보기용 가짜 fleet 생성
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

## 웹 모니터 (Docker)

데몬이 무엇을 하고 있는지 브라우저로 보는 대시보드입니다. 하네스 코드와 같은 저장소에 있고, Docker 로 띄웁니다.
표는 **AG Grid Community**, 차트는 **ApexCharts**(둘 다 jsDelivr CDN, 버전 고정)로 그립니다.

```bash
scripts/monitor.sh up        # 빌드 + 기동 → http://localhost:8787  (Windows 브라우저에서도 열림)
scripts/monitor.sh status    # 컨테이너 상태 + /api/health
scripts/monitor.sh logs      # 컨테이너 로그
scripts/monitor.sh restart   # 코드 수정 반영(다시 빌드)
scripts/monitor.sh down
scripts/monitor.sh demo      # 가짜 fleet 으로 미리보기(포트 8790) — 첫 실행 전에 화면을 보고 싶을 때. 해제: demo-down
scripts/monitor.sh local     # Docker 없이: uv sync --extra monitor 후 uv 가상환경으로 실행 (= hermes-fleet monitor)
```

| 탭 | 보여 주는 것 |
|---|---|
| 개요 | 데몬 상태(실행 중·대기·응답 없음·미실행·일시정지), 작동 시간대/다음 시작/머지 창, LLM 엔드포인트와 역할별 모델, 현재 처리 중인 티켓·단계. 차트: 티켓 상태 분포(도넛), 레포별 티켓 상태(누적 막대), 최근 14일 QA 결과·머지 건수. **레포 목록 그리드**: root 아래 모든 git 저장소와 선정/제외 사유, 브랜치·작업 트리·검증·티켓·처리 결과·마지막 머지 |
| 레포 상세 | 레포 목록에서 행을 클릭(또는 드롭다운)하면 그 레포의 **작업 내역**: 티켓별 단계(PM/디자인/개발/QA) 소요 시간, 이 레포의 일별 처리 결과, 처리 기록(state/<repo>.json), 티켓, 자동 머지 기록(merge-report.md), 이 레포의 데몬 로그 |
| 티켓 | 모든 레포의 백로그를 레포·상태·빠른 검색으로 필터. 티켓을 열면 수용 기준·본문·단계 타임라인(rangeBar)·`git diff --stat`·산출물(prd/design/dev-report/qa-report/summary). 산출물이 `hermes/<ID>` 브랜치에만 있으면 브랜치에서 읽는다(⎇ 표시) |
| 타임라인 | state/ 의 티켓 처리·merge-report 의 머지·daemon.log 의 ▶/■/✗ 마커를 시간순 그리드로 |
| 로그 | daemon.log 를 SSE 로 실시간 따라가기, 레포/문자열 필터, `[fleet]/[merge]` 줄만 보기 |

**레포 선택/해제**: 레포 목록의 토글(또는 레포 상세의 스위치)로 fleet 작업 대상을 켜고 끕니다. 이것은 `fleet.yaml` 의 `include`/`exclude` 를 주석을 보존하며 편집하는 것과 같습니다(해제 = `exclude` 에 추가, 선택 = `exclude` 에서 제거하고 `include` 를 쓰는 중이면 거기 추가).
FE/BE 가 확인되지 않는 저장소는 켜도 대상이 되지 않으므로 토글이 비활성화됩니다. 데몬은 매 tick(폴링 주기) 마다 `fleet.yaml` 변경을 감지해 include/exclude 를 다시 읽으므로, 돌고 있는 중에도 다음 레포 선택부터 반영됩니다(진행 중인 티켓은 마칩니다).

동작 방식과 제약:

- 컨테이너는 호스트 root(`/home/ubuntu`)를 **같은 경로에 읽기 전용**으로 마운트해 fleet.yaml·state/·logs/·각 레포의 git 을 그대로 읽고, `hermes-fleet` 디렉터리만 쓰기 가능으로 겹쳐 마운트합니다(레포 선택이 fleet.yaml 을 고치기 위해). 그 외에는 아무것도 쓰지 않으며 `approve/pause` 같은 조작은 제공하지 않습니다(CLI 사용).
- 데몬 실행 판정: `state/daemon.lock` 의 flock 이 잡혀 있으면 확정. Docker Desktop(별도 VM)에서는 호스트의 flock 이 보이지 않으므로 daemon.log 의 `[fleet] start` / `exiting` 마커와 최근 갱신 시각(폴링 주기의 3배, 최소 45분)으로 판단합니다. 종료 마커 없이 로그가 오래 멈추면 "응답 없음", 작동 시간대인데 시작 기록이 없으면 "미실행"으로 표시합니다.
- LLM 점검은 컨테이너에서 `host.docker.internal:11434`(Windows Ollama)로 나갑니다. 다른 위치면 `.env` 에 `MONITOR_OLLAMA_HOST` 또는 `MONITOR_HERMES_BASE_URL` 을 지정합니다.
- 설치 위치가 다르면 `FLEET_ROOT`(마운트할 root), `FLEET_DIR`(hermes-fleet 경로), `MONITOR_PORT`, `MONITOR_BIND`(기본 127.0.0.1; LAN 에 열려면 0.0.0.0), `TZ`(기본 Asia/Seoul — 데몬과 같은 시간대여야 시간대 판정이 맞음) 를 환경변수나 `.env` 로 넘깁니다.
- API: `/api/overview` `/api/stats` `/api/repos` `/api/repos/<repo>` `PUT /api/repos/<repo>/selection` `/api/repos/<repo>/tickets/<ID>` `/api/timeline` `/api/logs` `/api/logs/stream`(SSE), 문서는 `/api/docs`.

## 프런트엔드 기술 가이드 (크루 공통 규칙)

크루가 다른 레포의 프런트엔드를 만들거나 고칠 때 따르는 기술 스택은 [templates/org.yaml](templates/org.yaml) 의 `frontend_guide` 에 정의되어 있고,
PM·디자이너·개발자·QA 프롬프트와 Tech Lead 발굴 프롬프트에 **그대로 주입**됩니다(`hermes_fleet/prompts.py` 의 `render_frontend_guide`).

| 영역 | 사용 | 요지 |
|---|---|---|
| CSS | **Tailwind CSS** (v4) | 새 화면은 유틸리티 클래스로. 기존 CSS 변수는 theme 매핑으로 재사용. 정적 HTML 은 `@tailwindcss/browser@4.3.5` Play CDN |
| 폰트 | **Pretendard** (1.3.9) | `"Pretendard Variable", Pretendard, …` 스택. 다른 웹폰트 추가 금지 |
| 표 | **AG Grid Community** (34.2.0) | 정렬/필터/페이지네이션이 필요한 표. Enterprise 기능 금지, Theming API 사용. 5행 이하 정적 표는 `<table>` 허용 |
| 차트 | **ApexCharts** (4.7.0) 또는 **TradingView Lightweight Charts** (5.2.1) | 일반 차트는 ApexCharts, 캔들/OHLC/실시간 금융 차트는 Lightweight Charts. Chart.js·Highcharts·ECharts 등 추가 금지 |

- 도입 방식: 빌드 도구가 있으면 npm(+lock 갱신), 정적 HTML 이면 버전을 고정한 jsDelivr CDN. `latest` 금지.
- 디자이너는 스펙에 컴포넌트별 라이브러리와 Tailwind 토큰 매핑, 의존성(7절)을 명시하고, QA 는 FE diff 의 가이드 준수를 `FE-GUIDE` 행으로 검증해 위반 시 FAIL 사유로 삼습니다. Tech Lead 는 가이드와 어긋난 기존 화면을 작은 마이그레이션 티켓으로 제안할 수 있습니다.
- 바꾸려면 `templates/org.yaml` 의 `frontend_guide` 를 수정합니다(버전 올리기, 항목 추가). 레포별로 다르게 하려면 `<repo>/hermes/config/org.yaml` 에 `frontend_guide` 를 다시 정의하고, 끄려면 `frontend_guide: false` 를 둡니다(정의가 없으면 템플릿 것을 상속).
- 웹 모니터의 개요 > 설정 카드에서 현재 적용 중인 가이드 요약을 볼 수 있습니다.

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
