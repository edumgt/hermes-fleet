"""환경 초기화(`hermes-fleet init`): 받은 사람의 PC 에서 형제 레포를 다시 읽고 로컬 설정(fleet.yaml, .env)을 만든다.

공개 레포에는 개인 환경 정보(다른 레포 이름, 경로, 배포 대상)가 들어가지 않는다 — 그런 값은 전부 이 단계가 만드는
gitignore 된 fleet.yaml / .env / state/ 에만 존재한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .fleetfile import set_scalar
from .llm import check_endpoint
from .repos import FleetConfig, RepoProfile, scan_repos
from .settings import Settings, detect_ollama_host

ENV_KEYS = {"provider": "HERMES_LLM_PROVIDER", "model": "HERMES_MODEL", "base_url": "HERMES_BASE_URL", "api_key": "HERMES_API_KEY"}


@dataclass
class InitResult:
    fleet_dir: Path
    root: Path
    candidates: list[RepoProfile]
    selected: list[str]
    fleet_yaml: Path
    fleet_written: bool
    env_file: Path
    env_written: bool
    llm: dict[str, Any] = field(default_factory=dict)
    scaffolded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _set_env(text: str, key: str, value: str | None) -> str:
    if value is None:
        return text
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    return pat.sub(f"{key}={value}", text) if pat.search(text) else text.rstrip("\n") + f"\n{key}={value}\n"


def _render_fleet_yaml(example_text: str, root_value: str, include: list[str]) -> str:
    text = set_scalar(example_text, "root", root_value)
    block = "include: []" if not include else "include:\n" + "\n".join(f"  - {n}" for n in include)
    if re.search(r"^include:.*$", text, re.M):
        text = re.sub(r"^include:.*$", block, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + block + "\n"
    return text


def init_environment(fleet_dir: Path, root: Path | None = None, include: list[str] | None = None, select_all: bool = True,
                     provider: str | None = None, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                     force: bool = False, scaffold: bool = False, log: Callable[[str], None] = print,
                     endpoint_check: Callable[..., dict[str, Any]] = check_endpoint) -> InitResult:
    fleet_dir = fleet_dir.resolve()
    root = (root or fleet_dir.parent).resolve()
    example = fleet_dir / "fleet.example.yaml"
    target = fleet_dir / "fleet.yaml"
    env_example, env_file = fleet_dir / ".env.example", fleet_dir / ".env"
    notes: list[str] = []

    # 1) 환경 읽기: root 아래 git 저장소와 FE/BE 탐지
    log(f"① 환경 읽기 — root {root}")
    profiles = scan_repos(FleetConfig(root=root))
    candidates = [p for p in profiles if p.name != fleet_dir.name]
    eligible = [p for p in candidates if p.eligible]
    for p in candidates:
        mark = "●" if p.eligible else "○"
        fe = p.frontend.label() if p.frontend else "-"
        be = p.backend.label() if p.backend else "-"
        log(f"   {mark} {p.name:<28} FE {fe:<22} BE {be:<22} {'; '.join(p.reasons)}")
    if not candidates:
        notes.append("root 아래에 git 저장소가 없습니다. --root 로 레포들이 있는 폴더를 지정하세요.")
    if include:
        unknown = [n for n in include if n not in {p.name for p in candidates}]
        if unknown:
            raise ValueError(f"root 아래에 없는 레포: {unknown}")
        selected = list(include)
    else:
        selected = [p.name for p in eligible] if select_all else []
    log(f"   → 대상 {len(selected)}개: {', '.join(selected) or '(없음 — 웹 모니터 레포 목록에서 나중에 선택)'}")

    # 2) fleet.yaml
    root_value = "" if root == fleet_dir.parent else str(root)
    fleet_written = False
    if target.exists() and not force:
        notes.append(f"{target.name} 이 이미 있어 그대로 둡니다 (--force 로 다시 생성).")
    else:
        if not example.exists():
            raise FileNotFoundError(f"템플릿이 없습니다: {example}")
        target.write_text(_render_fleet_yaml(example.read_text(encoding="utf-8"), root_value, selected), encoding="utf-8")
        fleet_written = True
    log(f"② fleet.yaml — {'생성' if fleet_written else '유지'} ({target})")

    # 3) .env
    env_written = False
    if env_file.exists() and not force:
        notes.append(".env 가 이미 있어 그대로 둡니다 (--force 로 다시 생성).")
    else:
        text = env_example.read_text(encoding="utf-8") if env_example.exists() else ""
        for key, value in (("provider", provider), ("model", model), ("base_url", base_url), ("api_key", api_key)):
            text = _set_env(text, ENV_KEYS[key], value)
        env_file.write_text(text, encoding="utf-8")
        env_written = True
    log(f"③ .env — {'생성' if env_written else '유지'} ({env_file})")

    # 4) LLM 엔드포인트 점검 (실제 호출)
    prov = (provider or "ollama").lower()
    url = base_url or (f"http://{detect_ollama_host()}" if prov == "ollama" else "http://localhost:11434")
    settings = Settings(provider=prov, model=model or "hermes3:8b", base_url=url.rstrip("/"), api_key=api_key or "NA")
    info = endpoint_check(settings)
    if info.get("ok"):
        have = info.get("models") or []
        log(f"④ LLM — 연결됨 {info.get('url')} · 모델 {len(have)}개" + (f" · 선택 모델 {settings.model} {'있음' if settings.model in have or not have else '없음 → ollama pull ' + settings.model}" if prov == "ollama" else ""))
        if prov == "ollama" and have and settings.model not in have:
            notes.append(f"모델 {settings.model} 이 엔드포인트에 없습니다: ollama pull {settings.model}")
    else:
        log(f"④ LLM — 연결 실패 {info.get('url')}: {info.get('error')}")
        notes.append("LLM 엔드포인트에 연결하지 못했습니다. Ollama 를 실행하거나 .env 의 HERMES_BASE_URL 을 고치고 `hermes-fleet check` 로 다시 확인하세요.")

    # 5) 상태·로그 디렉터리
    for d in ("state", "logs"):
        (fleet_dir / d).mkdir(exist_ok=True)
    log("⑤ state/ logs/ 준비")

    # 6) (선택) 대상 레포에 hermes/ 스캐폴드
    scaffolded: list[str] = []
    if scaffold:
        from .fleet import ensure_scaffold  # noqa: WPS433

        for p in candidates:
            if p.name in selected and ensure_scaffold(p):
                scaffolded.append(p.name)
        log(f"⑥ 스캐폴드 — {', '.join(scaffolded) or '추가 없음'}")

    return InitResult(fleet_dir=fleet_dir, root=root, candidates=candidates, selected=selected, fleet_yaml=target, fleet_written=fleet_written,
                      env_file=env_file, env_written=env_written, llm=info, scaffolded=scaffolded, notes=notes)


def next_steps(res: InitResult) -> list[str]:
    steps = [
        "scripts/monitor.sh up          # 웹 모니터 http://127.0.0.1:8787 — 레포 목록에서 대상·승인 모드 확인",
        "scripts/hermes-fleet repos -v  # 대상 레포와 검증 명령",
        "scripts/hermes-fleet daemon --once --active-hours \"\"   # 지금 한 바퀴 돌려 보기(발굴 → 티켓)",
        "scripts/install-cron.sh        # 10분 간격 데몬 + 2분 간격 요청 러너 등록 (시간대는 fleet.yaml active_hours)",
    ]
    if not res.selected:
        steps.insert(0, "fleet.yaml 의 include 를 채우거나 웹 모니터 레포 목록에서 대상을 선택")
    return steps


def main(argv: list[str] | None = None) -> int:
    """`python -m hermes_fleet.setup` — crewai 가 없는 모니터 컨테이너에서도 동작하는 비대화 init."""
    import argparse

    ap = argparse.ArgumentParser(prog="hermes_fleet.setup", description="환경 초기화(비대화): 상위 폴더 스캔 → fleet.yaml/.env 생성 → LLM 점검")
    ap.add_argument("--fleet-dir", required=True)
    ap.add_argument("--root"); ap.add_argument("--include", action="append")
    ap.add_argument("--provider", choices=["ollama", "openai"]); ap.add_argument("--model"); ap.add_argument("--base-url"); ap.add_argument("--api-key")
    ap.add_argument("--force", action="store_true"); ap.add_argument("--scaffold", action="store_true"); ap.add_argument("--yes", "-y", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    include = [n for chunk in (a.include or []) for n in chunk.split(",") if n.strip()]
    res = init_environment(Path(a.fleet_dir), root=Path(a.root).expanduser() if a.root else None, include=include or None, provider=a.provider, model=a.model,
                           base_url=a.base_url, api_key=a.api_key, force=a.force, scaffold=a.scaffold)
    for n in res.notes:
        print(f"⚠ {n}")
    print("다음 단계:")
    for st in next_steps(res):
        print(f"  {st}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
