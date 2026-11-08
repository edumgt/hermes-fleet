# Hermes Fleet — 상위 폴더 다중 레포 하네스

Hermes(CrewAI) 가상 조직을 **클론한 폴더의 상위 디렉터리(root) 수준으로 올린 하네스**입니다.
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
│   ├── fleetfile.py      #   fleet.yaml include/exclude·스케줄 편집(웹 모니터의 레포 선택/스케줄 저장)
│   ├── requests.py       #   요청 큐(state/requests)와 레포별 일시정지(state/paused) — 웹/CLI 조작을 호스트 데몬이 실행
│   ├── workflow.py       #   전달 체인 탐지(원격·Actions·배포 흔적) + fleet 배포 실행 + Actions 상태 조회
│   ├── org.py / agents.py / prompts.py / backlog.py / llm.py / settings.py
│   ├── tools/            #   레포 루트로 샌드박싱된 fs / shell(허용 목록) / git
│   └── monitor/          #   웹 모니터(읽기 전용): collect.py(수집) · app.py(FastAPI) · static/(SPA)
├── docker-compose.yml / docker/monitor.Dockerfile   # 웹 모니터 컨테이너
├── scripts/hermes-fleet          # CLI 래퍼 (uv)
├── scripts/monitor.sh            # 웹 모니터 up/down/logs/status/local/demo
├── scripts/demo_fleet.py         # 미리보기용 가짜 fleet 생성
├── scripts/install-cron.sh       # 10분마다 기동 시도(flock); 시간대는 fleet.yaml active_hours 로 데몬이 판정 — WSL 기본
├── scripts/install-systemd.sh    # systemd 가 켜진 환경용(선택)
├── state/   (gitignore)  # fleet.json(커서), <repo>.json(처리 기록·머지 기록), PAUSE, paused/<repo>, requests/, daemon.lock
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

## 사람 승인 모드 (approval)

`fleet.yaml` `approval.default: auto|manual` 와 `approval.repos: {레포: 모드}` 로 레포 성격에 따라 고른다. **auto**(현재 모든 레포의 기본값) 는 Tech Lead 발굴 제안이 바로 `todo` 로 들어가 사람 승인 없이 크루가 진행하고, 남아 있던 `proposed` 티켓도 데몬이 다음 tick 에 자동 승인한다. **manual** 은 `proposed` 로 두고 웹의 ✓ 승인 또는 `hermes-fleet approve` 로 사람이 승인해야 진행한다. 웹 모니터 개요의 '기본 승인 모드' 와 레포 목록의 승인 열(자동/수동 클릭 전환, `*` = 레포 예외)에서 바꾸며 `PUT /api/approval`, `PUT /api/repos/{name}/approval` 로 저장된다. 발굴 상한(`discover.max_open_proposed`)은 auto 에서는 proposed+todo 합계로 센다.

## FE 가이드 준수 측정 (Playwright)

`scripts/monitor.sh qa` 는 docker compose 의 `playwright` 서비스(profile qa, `docker/playwright.Dockerfile`)로 모니터를 실제 Chromium 에서 열어 1920×1080(엄격)·1366×768(참고)에서 탭별 세로/가로 스크롤 없음·바닥 빈 띠 ≤ 80px, 상단 바 56px·사이드바 248/68px·트리거 16px·본문 여백 24/16px, 모달 정중앙(스케줄·확인·티켓)과 Esc 닫힘, 닫힌 모달 잔상 없음, 테마 토글, 브라우저 시간대가 달라도 KST 시계, 10px EDUMGT 푸터, Tailwind·Pretendard 로드, 콘솔 오류 없음을 채점한다. 결과는 `qa/out/report.json`, `qa/out/report.txt` 와 탭별 라이트/다크 스크린샷이며 FAIL 이 하나라도 있으면 종료 코드 1 이다(QA 의 VERDICT: FAIL 근거). `QA_MONITOR_URL` 로 다른 주소를, `QA_STRICT_WIDTHS` 로 엄격 폭을 바꾼다. `scripts/monitor.sh qa-shell` 은 컨테이너 셸이다.

설치 위치: `fleet.yaml` 의 `root` 를 비우면 hermes-fleet 의 상위 폴더를 스캔하고(상대 경로는 fleet.yaml 기준), `scripts/monitor.sh` 가 `FLEET_DIR`/`FLEET_ROOT` 를 자동으로 넘기므로 어디에 클론해도 같은 위치의 다른 레포가 대상이 된다.

## 공개 레포로 내보내기

추적 파일에는 환경 정보가 없지만(설정은 `fleet.yaml`/`.env`/`state/` 로 분리, 모두 gitignore), 이 저장소의 **기존 커밋 히스토리**에는 예전 fleet.yaml 과 README 의 레포 이름·배포 대상이 남아 있습니다. 그래서 공개는 히스토리를 자르고 합니다.

```bash
scripts/publish-check.sh                 # 추적 파일 + 현재 히스토리 점검 (히스토리에서 걸리면 아래로)
scripts/publish-check.sh --orphan public # 현재 추적 파일만 담은 단일 커밋 브랜치 'public' 생성(기존 브랜치·HEAD 유지) + 그 브랜치 점검
git push <public-remote> public:main     # 통과하면 그 브랜치만 공개 원격에 올린다
```

받은 사람은 `scripts/init.sh` 로 자기 환경(형제 레포, LLM)을 읽어 시작하고, 웹 모니터는 fleet.yaml 이 없으면 상단에 초기화 안내 배너를 띄우고 상위 폴더를 읽기 전용으로 미리 보여 줍니다.

## 하루 흐름 (cron 기준)

| 시각 | 동작 |
|---|---|
| 01:00~ | cron 은 하루 종일 10분마다 기동 시도(시간대 밖이면 데몬이 조용히 종료) → 시간대 시작 후 10분 안에 시작, 이미 돌고 있으면 flock 으로 무시. PC가 03:00에 켜지면 03:10 안에 시작 |
| 01:00~07:15 | 커서 다음 레포부터 `todo` 티켓이 있는 레포를 찾아 `hermes/<ID>` 브랜치에서 PM→Design→Dev⇄QA 실행, **브랜치에 커밋**(push 없음). QA PASS → `review`, FAIL/오류 → `blocked`. todo 가 모두 비면 Tech Lead 가 레포 하나를 분석해 `proposed` 티켓을 커밋 |
| 07:15~ | 새 작업 시작 안 함(`no_new_work_minutes_before_end: 45`). 진행 중 티켓은 마침 |
| 07:30 | 레포마다 하루 한 번 **자동 머지**: `git pull --rebase` → `review` 브랜치 `--no-ff` 머지 → 레포 프로필 테스트·빌드 실행 → 통과 시 티켓 `done`, `origin` push, 브랜치 삭제. 충돌/검증 실패는 되돌리고 `review` 로 남김 |
| 08:00 | 시간대 종료 → 데몬 종료. 이후 cron 이 깨워도 시간대 밖이라 즉시 종료(로그 없음) |
| 언제나 | 웹/CLI 요청(`state/requests/`)은 데몬이 매 tick 가장 먼저 처리. 시간대 밖에는 2분 간격 `daemon --on-demand` 러너(같은 flock)가 요청만 처리하고 종료 |

사람이 하는 일: 제안 승인(`approve`, approval 모드가 manual 일 때), 자동 머지 전 리뷰(막으려면 티켓을 `blocked` 로), blocked 티켓 처리.
레포가 기준 브랜치가 아닌 다른 브랜치에 있거나 작업 트리가 더러우면 **건드리지 않고 건너뜁니다**(사람이 작업 중인 것으로 간주).

## 설치 (받은 직후 — init 단계)

공개 레포에는 다른 레포 이름·경로·배포 대상이 들어 있지 않습니다. 그런 값은 전부 받은 사람의 환경에서 `init` 이 만드는 gitignore 파일(`fleet.yaml`, `.env`, `state/`)에만 존재합니다.

가장 짧은 길은 Docker 입니다. 작업 대상 레포들이 있는 폴더 안에 클론하고 그 폴더에서 `docker compose up -d` 를 실행하면, 모니터 컨테이너가 **기존 fleet.yaml 이 있으면 그대로 쓰고, 없으면 상위 폴더를 읽어 자동 init**(fleet.yaml·.env 생성, LLM 점검) 한 뒤 http://127.0.0.1:8787 에 뜹니다. LLM 이 Ollama 가 아니면 `HERMES_INIT_ARGS="--provider openai --base-url http://host.docker.internal:1234/v1 --model <model>" docker compose up -d` 처럼 넘기고, 자동 init 을 끄려면 `HERMES_INIT=skip`. 크루(데몬)는 호스트에서 돌므로 그다음 `scripts/install-cron.sh` 를 등록합니다.

```bash
git clone <this-repo> hermes-fleet && cd hermes-fleet      # 작업 대상 레포들이 있는 폴더 안에 클론한다 (형제 폴더가 후보가 된다)
docker compose up -d               # (Docker 경로) fleet.yaml 없으면 자동 init → 웹 모니터. 있으면 기존 설정 유지
scripts/init.sh                    # ① 상위 폴더의 git 저장소를 스캔해 FE/BE 탐지 → 대상 선택(대화형; --yes 면 전부)
                                   # ② fleet.example.yaml → fleet.yaml, .env.example → .env  ③ LLM 엔드포인트·모델 점검  ④ 다음 단계 안내
scripts/init.sh --yes --root /path/to/repos --provider openai --base-url http://localhost:1234/v1 --model qwen2.5-coder:7b   # 비대화 예
uv run pytest                      # 자체 테스트
scripts/hermes-fleet check         # 엔드포인트·모델·설정·대상 레포 점검
scripts/hermes-fleet repos -v      # 대상 레포와 검증 명령
scripts/install-cron.sh            # 10분 간격 데몬 기동 시도(시간대는 fleet.yaml active_hours) + 2분 간격 on-demand 러너(웹/CLI 요청 처리) 등록 (해제: --remove)
scripts/publish-check.sh           # (공개 전) 추적 파일·히스토리에 개인 환경 정보가 남았는지 점검
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
scripts/hermes-fleet daemon [--once] [--forever] [--only repo ...] [--active-hours ""] [--no-discover] [--on-demand]
scripts/hermes-fleet pause / resume [repo]                # 전체 또는 레포별 일시정지
scripts/hermes-fleet request <repo> run|discover|merge|approve|retry|stop [HX-nnn]   # 웹과 같은 요청 큐 (--list)
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
모니터 자체가 FE 가이드(templates/org.yaml frontend_guide)의 참조 구현이다: 공통 셸 = 상단 바 왼쪽 사이드바 트리거(데스크톱 패널 아이콘·모바일 햄버거, Ctrl/⌘+B, Alt+M) + 좌측 오프캔버스 LNB(유일한 1차 메뉴). 데스크톱 ≥1024px 은 펼침 248px ↔ 아이콘 레일 68px(툴팁·활성 막대·배지 점) push 모드로 `localStorage hermes.lnb` 에 기억, 모바일은 시트(dim·Esc/바깥 클릭 닫힘·포커스 트랩·선택 시 닫힘). 상태는 `<body data-lnb="open|rail|sheet|closed">` 하나로 표현하고 Tailwind `group-data` 변형으로 그린다(.top/main 의 push 오프셋만 CSS — 비계층 CSS 가 유틸리티보다 우선하기 때문). 하단에 데몬 상태·테마 토글(시스템→라이트→다크, `data-theme` + `localStorage hermes.theme`)·스케줄 설정·사용자. 경계·여백 토큰(사이드바 경계선에서 트리거 16px·본문 24px, 상하 16px, 카드 간격 12px·패딩 16px·반경 12px, z-index 30/40/50/60), 하단 10px 검정 EDUMGT 고정 푸터(본문·사이드바는 그 위에서 끝남), 테마 토글(시스템→라이트→다크, 토큰만으로 색 정의). 모든 시각은 한국 시간: 백엔드는 settings 임포트 시 TZ=Asia/Seoul 고정, 프런트는 Intl timeZone Asia/Seoul 로 표시(오프셋 없는 값은 KST 로 해석, GitHub 의 UTC 는 변환), cron 에 CRON_TZ=Asia/Seoul. 티켓 상태 색은 dataviz 검증기(인접쌍 CVD ΔE ≥ 7.2 + 범례·간격 보조 인코딩)를 통과한 팔레트. Tailwind CSS v4 Play CDN(@tailwindcss/browser@4.3.5, @theme 으로 기존 CSS 변수 매핑; 로드 실패 시 `html.no-tw` 최소 폴백 + 안내), Pretendard, AG Grid Community, ApexCharts, FHD 1920×1080 에서 모든 탭이 브라우저 세로 스크롤 없이 한 화면(긴 목록은 카드 내부 스크롤, 개요는 레포 목록이 남은 높이를 차지, 레포 상세는 패널 내부 스크롤), 중요 입출력은 모달(`<dialog>`: 스케줄·날짜별 시간대 편집, 티켓 상세, 중지/배포 확인 — 브라우저 confirm 미사용). 남은 작업: 수제 CSS(style.css)의 Tailwind 전면 전환은 hermes/backlog HX-001 티켓으로 크루에 제안되어 있다.


| 탭 | 보여 주는 것 |
|---|---|
| 개요 | 데몬 상태(실행 중·대기·응답 없음·미실행·일시정지), 작동 시간대/다음 시작/머지 창, LLM 엔드포인트와 역할별 모델, 현재 처리 중인 티켓·단계. 차트: 티켓 상태 분포(도넛), 레포별 티켓 상태(누적 막대), 최근 14일 QA 결과·머지 건수. **레포 목록 그리드**: root 아래 모든 git 저장소와 선정/제외 사유, 브랜치·작업 트리·검증·티켓·처리 결과·마지막 머지 |
| 크루 룸 | 레포별로 PM·디자이너·개발자·QA·Tech Lead 를 **3D 사람 아바타**(Three.js, canvas)로 세워 두고 daemon.log 를 역할별 대화로 바꿔 보여 준다. 작업 중인 역할은 키보드를 치고 모니터가 켜지며, 산출물(Final Answer)·도구 호출·QA 판정·제안은 머리 위 말풍선과 오른쪽 **아바타 대화방**에 흐른다. 완료는 손을 들고, 실패/결함은 머리를 젓는다. 레포 선택·활동 레포 자동 전환·아바타 클릭(역할 필터). `/api/crew`, SSE `/api/crew/stream` (`hermes_fleet/monitor/crew.py`) |
| 레포 상세 | 레포 목록에서 행을 클릭(또는 드롭다운)하면 그 레포의 **작업 내역**: 티켓별 단계(PM/디자인/개발/QA) 소요 시간, 이 레포의 일별 처리 결과, 처리 기록(state/<repo>.json), 티켓, 자동 머지 기록(merge-report.md), 이 레포의 데몬 로그 |
| 티켓 | 모든 레포의 백로그를 레포·상태·빠른 검색으로 필터. 티켓을 열면 수용 기준·본문·단계 타임라인(rangeBar)·`git diff --stat`·산출물(prd/design/dev-report/qa-report/summary). 산출물이 `hermes/<ID>` 브랜치에만 있으면 브랜치에서 읽는다(⎇ 표시) |
| 워크플로 | 레포마다 **발굴 → 승인 → 크루 → 리뷰 → 머지·검증 → GitHub push → CI/CD(Actions) → 배포(AWS…)** 8단계 보드. 각 셀은 그 단계가 이 레포에서 자동인지(파랑) · 사람이 하는지(빗금) · 해당 없는지(점선)를, 점은 최근 상태(티켓 수, 마지막 머지/push, Actions 결과, 배포 결과)를 보여 준다. 레포 상세에도 같은 체인과 원격·워크플로·배포 흔적·Actions 최근 실행이 나온다 |
| 타임라인 | state/ 의 티켓 처리·merge-report 의 머지·daemon.log 의 ▶/■/✗ 마커를 시간순 그리드로 |
| 로그 | daemon.log 를 SSE 로 실시간 따라가기, 레포/문자열 필터, `[fleet]/[merge]` 줄만 보기 |

**제어(웹에서 조작)**: 개요의 제어 카드와 레포 목록의 버튼, 레포 상세·티켓 상세의 버튼으로 다음을 할 수 있습니다.

| 조작 | 하는 일 | 반영 |
|---|---|---|
| 스케줄 저장 | 작동 시간대·자동 머지 시각·종료 전 여유(분)·폴링 주기를 `fleet.yaml` 에 저장 | 데몬이 돌고 있으면 다음 tick 에 다시 읽음. cron 은 시간을 고정하지 않으므로 새 시간대 안이면 10분 안에 데몬이 시작되고, 밖이면 종료 |
| 날짜별 시간대 달력 | 개요 > 제어의 달력에서 날짜를 골라(클릭·Shift 범위) '시간대 지정 / 쉬는 날 / 기본으로 되돌림' 을 적용 → `fleet.yaml` `calendar:` 블록에 저장 (`PUT /api/calendar`) | 데몬은 `Schedule`(hermes_fleet/schedule.py) 로 '오늘 날짜의 예외 → 없으면 active_hours' 순으로 판정. 자정을 넘는 창은 다음 날 새벽까지 이어지고, off 인 날은 새 창을 열지 않는다. 다음 tick 에 핫리로드 |
| 전체 일시정지/재개 | `state/PAUSE` 생성/삭제 | 데몬이 새 작업·요청을 시작하지 않음(진행 중 티켓은 마침) |
| 레포 ▶ 실행 / 🔍 발굴 / ⇅ 머지 | `state/requests/` 에 요청 파일을 쌓음 | 야간 데몬은 매 tick 요청을 먼저 처리하고, 시간대 밖에는 cron 의 2분 간격 `daemon --on-demand` 러너가 처리 |
| 레포 ⏹ 중지 / ▶ 재개 | `stop` 요청 + `state/paused/<repo>` 마커 | 진행 중 크루는 다음 단계 경계에서 중단(티켓 blocked, 부분 작업 커밋), 레포는 재개 전까지 건너뜀 |
| 티켓 ✓ 승인 / ↺ 재시도 준비 / ▶ 이 티켓 실행 | `approve`(proposed→todo) · `retry`(blocked→todo, 기록 삭제) · `run HX-nnn` 요청 | 호스트 러너가 레포에 커밋/실행 |

컨테이너는 다른 레포에 쓸 수 없고 LLM 크루도 없으므로 **모든 조작은 요청 파일로 남고 호스트의 데몬이 실행**합니다. 요청 큐 카드에서 대기·실행 중·최근 결과(done/failed/skipped 와 사유)를 보고 대기 중인 요청은 취소할 수 있습니다.
요청이 바로 처리되려면 `scripts/install-cron.sh` 로 on-demand 러너 cron 줄이 등록되어 있어야 합니다(없으면 야간 데몬이 처리). 즉시 처리: `scripts/hermes-fleet daemon --on-demand`.

**전개 범위 판단(워크플로 탭)**: 레포의 성격은 자동 탐지합니다 — 원격(`origin`) 이 있으면 push 단계, `.github/workflows` 에 기준 브랜치 push 에 반응하는 워크플로가 있으면 CI/CD 단계, 그 중 이름·잡·내용이 배포성(deploy/cd/release, `aws-actions/*`, ssh→EC2, ECR, S3 sync, SAM/CDK…)이면 "배포(Actions 자동)", 워크플로는 없지만 배포 스크립트·IaC(`deploy/`, `*.sh` 의 `aws s3 sync`, `serverless.yml`, `samconfig.toml`, `cdk.json`, `buildspec.yml`, `terraform/`, `appspec.yml`…)가 있으면 "배포(수동)". AWS 흔적이 있으면 AWS 뱃지가 붙습니다.
`fleet.yaml` 의 `repos.<name>.deploy` 로 명시할 수도 있습니다: `{target: aws-s3-cloudfront, trigger: actions|fleet|manual|none, command: "aws s3 sync … && aws cloudfront create-invalidation …", cwd: ., note: …}`.
`trigger: fleet` + `command` 이면 **일일 머지가 push 에 성공한 뒤 fleet 이 그 명령을 실행**해 결과를 state 에 기록하고(merge-report 에도 남음), `command` 가 있으면 웹의 "🚀 배포 요청"(요청 큐 `deploy`)으로도 호스트에서 실행할 수 있습니다. GitHub Actions 상태는 공개 레포는 바로, 비공개 레포는 `.env` 의 `MONITOR_GITHUB_TOKEN`(repo 읽기 권한)으로 조회합니다.

**레포 선택/해제**: 레포 목록의 토글(또는 레포 상세의 스위치)로 fleet 작업 대상을 켜고 끕니다. 이것은 `fleet.yaml` 의 `include`/`exclude` 를 주석을 보존하며 편집하는 것과 같습니다(해제 = `exclude` 에 추가, 선택 = `exclude` 에서 제거하고 `include` 를 쓰는 중이면 거기 추가).
FE/BE 가 확인되지 않는 저장소는 켜도 대상이 되지 않으므로 토글이 비활성화됩니다. 데몬은 매 tick(폴링 주기) 마다 `fleet.yaml` 변경을 감지해 include/exclude 를 다시 읽으므로, 돌고 있는 중에도 다음 레포 선택부터 반영됩니다(진행 중인 티켓은 마칩니다).

동작 방식과 제약:

- 컨테이너는 호스트 root(hermes-fleet 의 상위 폴더)를 **같은 경로에 읽기 전용**으로 마운트해 fleet.yaml·state/·logs/·각 레포의 git 을 그대로 읽고, `hermes-fleet` 디렉터리만 쓰기 가능으로 겹쳐 마운트합니다. 컨테이너가 쓰는 것은 `fleet.yaml`(선택·스케줄), `state/PAUSE`, `state/paused/`, `state/requests/` 뿐입니다.
- 데몬 실행 판정: `state/daemon.lock` 의 flock 이 잡혀 있으면 확정. Docker Desktop(별도 VM)에서는 호스트의 flock 이 보이지 않으므로 daemon.log 의 `[fleet] start` / `exiting` 마커와 최근 갱신 시각(폴링 주기의 3배, 최소 45분)으로 판단합니다. 종료 마커 없이 로그가 오래 멈추면 "응답 없음", 작동 시간대인데 시작 기록이 없으면 "미실행"으로 표시합니다.
- LLM 점검은 컨테이너에서 `host.docker.internal:11434`(Windows Ollama)로 나갑니다. 다른 위치면 `.env` 에 `MONITOR_OLLAMA_HOST` 또는 `MONITOR_HERMES_BASE_URL` 을 지정합니다.
- 설치 위치가 다르면 `FLEET_ROOT`(마운트할 root), `FLEET_DIR`(hermes-fleet 경로), `MONITOR_PORT`, `MONITOR_BIND`(기본 127.0.0.1; LAN 에 열려면 0.0.0.0), `TZ`(기본 Asia/Seoul — 데몬과 같은 시간대여야 시간대 판정이 맞음) 를 환경변수나 `.env` 로 넘깁니다.
- API: `/api/overview` `/api/stats` `/api/workflow?ci=true` `/api/repos` `/api/repos/<repo>` `PUT /api/repos/<repo>/selection` `/api/repos/<repo>/tickets/<ID>` `/api/timeline` `/api/logs` `/api/logs/stream`(SSE), 문서는 `/api/docs`.

## 프런트엔드 기술 가이드 (크루 공통 규칙)

크루가 다른 레포의 프런트엔드를 만들거나 고칠 때 따르는 기술 스택은 [templates/org.yaml](templates/org.yaml) 의 `frontend_guide` 에 정의되어 있고,
PM·디자이너·개발자·QA 프롬프트와 Tech Lead 발굴 프롬프트에 **그대로 주입**됩니다(`hermes_fleet/prompts.py` 의 `render_frontend_guide`).

| 영역 | 사용 | 요지 |
|---|---|---|
| CSS | **Tailwind CSS** (v4) | 새 화면은 유틸리티 클래스로. 기존 CSS 변수는 theme 매핑으로 재사용. 정적 HTML 은 `@tailwindcss/browser@4.3.5` Play CDN |
| 폰트 | **Pretendard** (1.3.9) | `"Pretendard Variable", Pretendard, …` 스택. 다른 웹폰트 추가 금지 |
| 레이아웃 | **오프캔버스 좌측 사이드바 + 햄버거 메뉴** (ChatGPT 방식) | 모든 화면이 같은 셸 안에. 상단 바 왼쪽 ☰ 버튼이 유일한 1차 메뉴를 연다. 데스크톱은 push(열림 기억), 모바일은 overlay. Tailwind 로 구현, 별도 UI 프레임워크 금지 |
| 표 | **AG Grid Community** (34.2.0) | 정렬/필터/페이지네이션이 필요한 표. Enterprise 기능 금지, Theming API 사용. 5행 이하 정적 표는 `<table>` 허용 |
| 차트 | **ApexCharts** (4.7.0) 또는 **TradingView Lightweight Charts** (5.2.1) | 일반 차트는 ApexCharts, 캔들/OHLC/실시간 금융 차트는 Lightweight Charts. Chart.js·Highcharts·ECharts 등 추가 금지 |

- 도입 방식: 빌드 도구가 있으면 npm(+lock 갱신), 정적 HTML 이면 버전을 고정한 jsDelivr CDN. `latest` 금지.
- 디자이너는 스펙에 컴포넌트별 라이브러리와 Tailwind 토큰 매핑, 의존성(7절)을 명시하고, QA 는 FE diff 의 가이드 준수를 `FE-GUIDE` 행으로 검증해 위반 시 FAIL 사유로 삼습니다. Tech Lead 는 가이드와 어긋난 기존 화면을 작은 마이그레이션 티켓으로 제안할 수 있습니다.
- 바꾸려면 `templates/org.yaml` 의 `frontend_guide` 를 수정합니다(버전 올리기, 항목 추가). 레포별로 다르게 하려면 `<repo>/hermes/config/org.yaml` 에 `frontend_guide` 를 다시 정의하고, 끄려면 `frontend_guide: false` 를 둡니다(정의가 없으면 템플릿 것을 상속).
- 웹 모니터의 개요 > 설정 카드에서 현재 적용 중인 가이드 요약을 볼 수 있습니다.

## 레포 안의 규약

- 티켓: `<repo>/hermes/backlog/HX-nnn-*.md` (YAML frontmatter + 본문). `area` 는 frontend|backend|signaling|fullstack|api|db|infra|docs.
- 산출물: `<repo>/hermes/workspace/<ID>/` (prd.md, design.md, dev-report.md, qa-report.md, summary.md). 로그·발굴 원문은 gitignore.
- 조직 오버라이드: `<repo>/hermes/config/org.yaml` (없으면 templates/org.yaml 을 상속).
- 브랜치 `hermes/<ID>`, 커밋 서명 `Co-Authored-By: Hermes Fleet`.

## hermes-fleet 자신도 크루의 대상

`fleet.yaml` 의 `repos.hermes-fleet` 로 하네스 자신을 대상에 넣었습니다(FE = `hermes_fleet/monitor/static`, BE = `.`, 검증 = `uv run --with pytest python -m pytest -q`).
크루가 데몬의 상태·설정을 건드리지 못하도록 `deny: [state/, logs/, fleet.yaml, …]` 가 fs 도구 샌드박스에 추가되고, 데몬은 시작 시 자신의 모듈을 모두 적재해 브랜치 전환 중 지연 import 로 깨지지 않게 했습니다.
주의할 점: 티켓은 `hermes/HX-nnn` 브랜치에서 처리되고 **07:30 자동 머지로 main 에 들어간 코드가 다음 데몬 기동부터 실행**됩니다. 테스트가 통과했더라도 데몬 동작을 바꾸는 변경은 머지 전(`review` 상태)에 `git diff main..hermes/HX-nnn` 으로 확인하고, 막으려면 티켓을 `blocked` 로 바꾸거나 웹에서 레포를 ⏹ 중지하세요. 작업 트리가 더러우면(개발 중) 데몬은 이 레포를 건너뜁니다.

## 안전장치

- 에이전트 도구는 레포 루트로 샌드박싱: `.env` `.git` `node_modules` `dist` `target` 접근 불가, 셸은 허용 목록(npm/npx/node, pytest/uv run, mvn, 읽기 전용 git/ls/cat/grep)만, `rm sudo curl docker aws pip` 등 금지.
- push 는 오직 일일 머지 단계에서, 검증 통과분만. `auto_merge.require_verification: true` 면 실행 가능한 테스트/빌드가 없는 레포는 자동 머지하지 않습니다(`repos -v` 의 사유에 표시).
- 전체 중지: `scripts/hermes-fleet pause` 또는 `scripts/install-cron.sh --remove`. 특정 레포 제외: `fleet.yaml` `exclude`.

## 한계

- 8B 모델 특성상 긴 파일 재작성에서 실수할 수 있습니다. 티켓을 작게 유지하고 수용 기준을 명확히 쓰는 편이 효과적입니다.
- Java(Spring) 레포는 `mvn test` 가 의존성을 내려받아 첫 실행이 느립니다. 정적 HTML 프런트엔드는 테스트가 없어 BE 검증에만 의존합니다.
- 추론은 Windows Ollama 에서 일어나므로 GPU 메모리를 비워 두면 빠릅니다. 동시 처리는 하지 않고 한 번에 한 레포·한 티켓만 처리합니다.
