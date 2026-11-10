"""웹에서 받은 장문 프로젝트 요구사항을 레포별 실행 백로그로 전환한다.

원문은 fleet state 아래에 보관하고, 모델은 원문과 실제 저장소 구조를 함께 읽어
작고 검증 가능한 Hermes 티켓으로 분해한다. 원문 등록만으로 코드를 수정하지 않으며,
생성된 티켓은 항상 proposed 상태에서 사람이 승인해야 실행된다.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from .agents import build_agent, build_tools
from .discover import parse_proposals, repo_digest, write_proposals
from .llm import build_llm
from .org import load_org_for
from .repos import RepoProfile
from .settings import Settings

_ID = re.compile(r"[^A-Za-z0-9._-]+")
MAX_BRIEF_CHARS = 60_000


def directory(state_dir: Path) -> Path:
    return state_dir / "enhancements"


def _path(state_dir: Path, enhancement_id: str) -> Path:
    safe = _ID.sub("", enhancement_id)
    if not safe:
        raise ValueError("invalid enhancement id")
    return directory(state_dir) / f"{safe}.json"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    payload.setdefault("id", path.stem)
    return payload


def list_enhancements(state_dir: Path, repo: str | None = None) -> list[dict[str, Any]]:
    items = [x for x in (_read(p) for p in directory(state_dir).glob("*.json")) if x]
    if repo:
        items = [x for x in items if x.get("repo") == repo]
    return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)


def get_enhancement(state_dir: Path, enhancement_id: str) -> dict[str, Any]:
    record = _read(_path(state_dir, enhancement_id))
    if not record:
        raise ValueError(f"기능 강화 프로젝트를 찾을 수 없습니다: {enhancement_id}")
    return record


def create_enhancement(state_dir: Path, repo: str, brief: str, title: str = "") -> dict[str, Any]:
    brief = brief.strip()
    if len(brief) < 40:
        raise ValueError("요구사항은 40자 이상 입력하세요")
    if len(brief) > MAX_BRIEF_CHARS:
        raise ValueError(f"요구사항은 {MAX_BRIEF_CHARS:,}자 이하여야 합니다")
    now = time.time()
    eid = f"PX-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{int((now % 1) * 1000):03d}"
    record = {
        "id": eid,
        "repo": repo,
        "title": title.strip() or brief.splitlines()[0].strip()[:80] or "기능 강화 프로젝트",
        "brief": brief,
        "status": "draft",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "tickets": [],
    }
    _write(_path(state_dir, eid), record)
    return record


def update_enhancement(state_dir: Path, enhancement_id: str, **changes: Any) -> dict[str, Any]:
    record = get_enhancement(state_dir, enhancement_id)
    record.update(changes)
    record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write(_path(state_dir, enhancement_id), record)
    return record


def _prompt(profile: RepoProfile, brief: str, digest: str) -> str:
    return f"""
당신은 '{profile.name}' 저장소의 제품/기술 리드입니다. 아래 고객 요구사항을 해당 저장소에서 실제로 구현할 수 있는 단계적 백로그로 분해하세요.

## 절대 규칙
- 요구사항 원문은 고객의 권위 있는 범위다. 임의로 기능을 빼거나 다른 프로젝트를 제안하지 않는다.
- 현재 저장소의 구조와 기술 스택에 맞는 변경만 제안한다. 이미 있는 기능은 확장·연결 관점으로 제안한다.
- V1은 한 번에 거대한 재작성으로 만들지 말고, 의존 관계가 분명한 작고 검증 가능한 티켓으로 쪼갠다.
- 개인정보·권한·외부 API·알림 발송이 포함되면 안전한 입력/오류/재처리/감사 이력을 수용 기준에 포함한다.
- 화면 작업은 공통 frontend_guide의 FHD·폼 밀도·모달·테마 규칙을 지켜야 한다.
- 최대 6개만 제안하고, 각 티켓은 구현 가능한 순서로 작성한다. 파일 경로는 실제 파일만 인용한다.

## 대상 저장소
{profile.profile_text()}

## 저장소 구조 요약
{digest}

## 고객 요구사항 원문
{brief}

## 출력 형식
설명 없이 YAML 배열 하나만 출력한다.
```yaml
- title: 60자 이내의 구현 단위 제목
  priority: P0|P1|P2|P3
  area: frontend|backend|fullstack|api|db|infra|docs
  stages: [pm, design, dev, qa]
  acceptance:
    - 검증 가능한 완료 조건
    - 오류·권한·재처리 등의 완료 조건
  background: 요구사항 원문과 현재 코드의 연결 근거
  scope:
    - 구현 범위
  files:
    - 실제 관련 파일 경로
```
"""


def plan_enhancement(profile: RepoProfile, state_dir: Path, enhancement_id: str, settings: Settings | None = None,
                     log: Callable[[str], None] = print) -> list[Path]:
    """장문 요구사항을 proposed 티켓으로만 생성한다. 코드 실행은 승인 후 기존 run 흐름이 맡는다."""
    from crewai import Crew, Process, Task  # noqa: WPS433

    record = get_enhancement(state_dir, enhancement_id)
    if record.get("repo") != profile.name:
        raise ValueError("요청 레포와 기능 강화 프로젝트의 레포가 다릅니다")
    settings = settings or Settings.from_env()
    update_enhancement(state_dir, enhancement_id, status="planning")
    org = load_org_for(profile)
    llm = build_llm(settings)
    tools = build_tools(profile.root, settings, deny=profile.deny)
    agent = build_agent(org.get("discover", {}).get("role", "techlead"), org, llm, tools, settings=settings)
    task = Task(description=_prompt(profile, record["brief"], repo_digest(profile)), expected_output="YAML backlog proposals", agent=agent)
    output = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=True).kickoff()
    raw = getattr(output, "raw", None) or str(output)
    archive = profile.workspace_dir / "enhancements" / enhancement_id
    archive.mkdir(parents=True, exist_ok=True)
    requirements = archive / "requirements.md"
    requirements.write_text(f"# {record['title']}\n\n{record['brief']}\n", encoding="utf-8")
    (archive / "planning-output.md").write_text(raw, encoding="utf-8")
    proposals = parse_proposals(raw)[:6]
    if not proposals:
        update_enhancement(state_dir, enhancement_id, status="failed", error="모델 출력에서 티켓을 읽지 못했습니다")
        raise ValueError("모델 출력에서 실행 티켓을 만들지 못했습니다")
    created = write_proposals(proposals, profile.backlog_dir, log=log, repo_root=profile.root, status="proposed")
    for ticket_path in created:
        with ticket_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n## 기능 강화 프로젝트\n\n- 프로젝트 ID: {enhancement_id}\n- 요구사항 원문: `hermes/workspace/enhancements/{enhancement_id}/requirements.md`\n- 이 원문을 범위 판단의 최우선 기준으로 사용한다.\n")
    update_enhancement(state_dir, enhancement_id, status="ready" if created else "failed",
                       tickets=[p.name.split("-", 2)[0] + "-" + p.name.split("-", 2)[1] for p in created],
                       artifact_dir=str(archive.relative_to(profile.root)),
                       error="" if created else "중복 또는 검증 실패로 생성된 티켓이 없습니다")
    return created
