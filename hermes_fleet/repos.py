"""저장소 탐지와 프로필: root 아래 git 레포 중 프런트엔드(FE)와 백엔드(BE)가 모두 분명한 것만 작업 대상으로 고른다.

각 사이드(FE/BE)의 스택(node | static | python | java)에 따라 의존성 설치·테스트·빌드 명령을 결정한다.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .settings import REPO_BACKLOG_DIR, REPO_ORG_OVERRIDE, REPO_WORKSPACE_DIR
from .tools import git

FE_DIR_NAMES = ("frontend", "fe", "client", "web", "ui", "www")
BE_DIR_NAMES = ("backend", "server", "api", "signaling", "app", "services")
FE_PACKAGE_HINTS = ("vite", "react", "vue", "svelte", "next", "@angular/core", "tailwindcss", "webpack", "parcel")
IGNORE_DIRS = {".git", "node_modules", "dist", "target", ".venv", "venv", "__pycache__", "docs", "scripts", "data", "infra", "deploy", "hermes"}


@dataclass
class Side:
    """FE 또는 BE 한 쪽의 위치와 스택."""
    dir: str                      # 저장소 루트 기준 상대 경로 ('.' 가능)
    kind: str                     # node | static | python | java
    tests_dir: str | None = None
    deps_cmd: list[str] | None = None     # node_modules 없을 때 등 사전 설치
    test_cmd: list[str] | None = None
    build_cmd: list[str] | None = None
    notes: list[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.dir} ({self.kind})"

    def commands_text(self) -> str:
        parts = []
        if self.deps_cmd:
            parts.append(f"의존성: `{' '.join(self.deps_cmd)}` (cwd: {self.dir})")
        if self.test_cmd:
            parts.append(f"테스트: `{' '.join(self.test_cmd)}` (cwd: {self.dir})")
        if self.build_cmd:
            parts.append(f"빌드: `{' '.join(self.build_cmd)}` (cwd: {self.dir})")
        if not self.test_cmd and not self.build_cmd:
            parts.append("자동 실행 가능한 테스트/빌드 없음 → 변경 파일을 읽어 정적으로 검토하고 필요하면 테스트를 새로 만든다")
        return "; ".join(parts)


@dataclass
class RepoProfile:
    name: str
    root: Path
    frontend: Side | None = None
    backend: Side | None = None
    base_branch: str = "main"
    has_remote: bool = False
    reasons: list[str] = field(default_factory=list)   # 선정/제외 사유
    deny: tuple[str, ...] = ()                         # 에이전트 도구가 건드리면 안 되는 레포 내 경로 (fleet.yaml repos.<name>.deny)
    deploy: dict[str, Any] = field(default_factory=dict)   # fleet.yaml repos.<name>.deploy: {target, trigger: actions|fleet|manual|none, command, cwd, note}
    approval: str = "auto"                              # 사람 승인 모드: auto(제안 → 바로 todo) | manual(proposed → 사람 승인). fleet.yaml approval 블록

    @property
    def eligible(self) -> bool:
        return self.frontend is not None and self.backend is not None

    @property
    def backlog_dir(self) -> Path:
        return self.root / REPO_BACKLOG_DIR

    @property
    def workspace_dir(self) -> Path:
        return self.root / REPO_WORKSPACE_DIR

    @property
    def org_override(self) -> Path:
        return self.root / REPO_ORG_OVERRIDE

    def verify_steps(self) -> list[tuple[str, list[str]]]:
        """머지 검증용 (cwd, command) 목록. 사전 설치 명령 포함."""
        steps: list[tuple[str, list[str]]] = []
        for side in (self.backend, self.frontend):
            if side is None:
                continue
            if side.deps_cmd:
                steps.append((side.dir, side.deps_cmd))
            if side.test_cmd:
                steps.append((side.dir, side.test_cmd))
            if side.build_cmd:
                steps.append((side.dir, side.build_cmd))
        return steps

    def has_verification(self) -> bool:
        return any(s.test_cmd or s.build_cmd for s in (self.frontend, self.backend) if s)

    def describe(self) -> str:
        fe = self.frontend.label() if self.frontend else "(없음)"
        be = self.backend.label() if self.backend else "(없음)"
        return f"{self.name}: FE={fe}, BE={be}, base={self.base_branch}, remote={'yes' if self.has_remote else 'no'}"

    def profile_text(self) -> str:
        """프롬프트에 넣는 저장소 프로필."""
        lines = [f"- 저장소: {self.name} (기준 브랜치 {self.base_branch})"]
        if self.frontend:
            lines.append(f"- 프런트엔드: {self.frontend.label()} — {self.frontend.commands_text()}")
        if self.backend:
            lines.append(f"- 백엔드: {self.backend.label()} — {self.backend.commands_text()}")
        return "\n".join(lines)


# ---------- 스택 판별 ----------

def _read_package(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads((path / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _node_side(dir_rel: str, path: Path) -> Side:
    pkg = _read_package(path) or {}
    scripts = pkg.get("scripts", {})
    lock = (path / "package-lock.json").exists()
    side = Side(dir=dir_rel, kind="node", deps_cmd=["npm", "ci", "--no-audit", "--no-fund"] if lock else ["npm", "install", "--no-audit", "--no-fund"])
    if "test" in scripts and "no test specified" not in str(scripts["test"]):
        side.test_cmd = ["npm", "test"]
    if "build" in scripts:
        side.build_cmd = ["npm", "run", "build"]
    for cand in ("test", "tests", "src", "__tests__"):
        if (path / cand).is_dir():
            side.tests_dir = f"{dir_rel}/{cand}".replace("./", "")
            break
    return side


def _python_side(dir_rel: str, path: Path, repo_root: Path) -> Side:
    side = Side(dir=dir_rel, kind="python")
    tests = next((c for c in ("tests", "test") if (path / c).is_dir()), None)
    if tests is None and dir_rel != "." and (repo_root / "tests").is_dir():
        tests, path_for_tests = "tests", repo_root
    else:
        path_for_tests = path
    if tests:
        side.tests_dir = (Path(dir_rel) / tests).as_posix() if path_for_tests == path else tests
    if (path / "pyproject.toml").exists():
        side.test_cmd = ["uv", "run", "--with", "pytest", "python", "-m", "pytest", "-q"] if tests else None
        if tests and path_for_tests != path:
            side.test_cmd += [str(Path("..") / tests)]
    else:
        req = next((p for p in (path / "requirements.txt", repo_root / "requirements.txt") if p.exists()), None)
        if req and tests:
            rel_req = req.relative_to(repo_root).as_posix()
            rel_tests = side.tests_dir
            # 저장소 루트에서 실행해 경로가 단순해지도록 cwd 를 '.' 로 둔다
            side.dir = "."
            side.notes.append(f"source dir: {dir_rel}")
            side.test_cmd = ["uv", "run", "--python", "3.12", "--with", "pytest", "--with-requirements", rel_req, "python", "-m", "pytest", "-q", rel_tests]
        side.notes.append("requirements.txt 기반" if req else "requirements 없음")
    return side


def _java_side(dir_rel: str, path: Path) -> Side:
    side = Side(dir=dir_rel, kind="java")
    if (path / "mvnw").exists():
        side.test_cmd = ["./mvnw", "-q", "-B", "test"]
    elif shutil.which("mvn"):
        side.test_cmd = ["mvn", "-q", "-B", "test"]
    else:
        side.notes.append("mvn 없음 → 검증 불가")
    if (path / "src" / "test").is_dir():
        side.tests_dir = f"{dir_rel}/src/test".replace("./", "")
    return side


def _static_side(dir_rel: str) -> Side:
    return Side(dir=dir_rel, kind="static", notes=["정적 HTML/JS (빌드 도구 없음)"])


def classify_frontend(repo_root: Path, dir_rel: str) -> Side | None:
    path = repo_root / dir_rel
    if not path.is_dir():
        return None
    pkg = _read_package(path)
    if pkg:
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if dir_rel != "." or any(h in deps for h in FE_PACKAGE_HINTS):
            return _node_side(dir_rel, path)
        return None
    has_markup = any(path.glob("*.html")) or (path / "public").is_dir() or (path / "src").is_dir()
    if has_markup and dir_rel != ".":
        return _static_side(dir_rel)
    return None


def classify_backend(repo_root: Path, dir_rel: str) -> Side | None:
    path = repo_root / dir_rel
    if not path.is_dir():
        return None
    if (path / "pom.xml").exists() and (path / "src" / "main").is_dir():
        return _java_side(dir_rel, path)
    if (path / "build.gradle").exists() or (path / "build.gradle.kts").exists():
        return Side(dir=dir_rel, kind="java", test_cmd=["./gradlew", "test"] if (path / "gradlew").exists() else None)
    py_markers = ("requirements.txt", "pyproject.toml", "main.py", "manage.py")
    if any((path / m).exists() for m in py_markers) or (dir_rel != "." and (repo_root / "requirements.txt").exists() and any(path.glob("*.py"))):
        return _python_side(dir_rel, path, repo_root)
    pkg = _read_package(path)
    if pkg and dir_rel != ".":
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if not any(h in deps for h in FE_PACKAGE_HINTS):
            return _node_side(dir_rel, path)
    return None


def _candidate_dirs(repo_root: Path) -> list[str]:
    return sorted(p.name for p in repo_root.iterdir() if p.is_dir() and p.name not in IGNORE_DIRS and not p.name.startswith("."))


def detect_profile(repo_root: Path, override: dict[str, Any] | None = None) -> RepoProfile:
    """한 저장소의 FE/BE 를 찾는다. override: {frontend: dir, backend: dir}."""
    override = override or {}
    profile = RepoProfile(name=repo_root.name, root=repo_root)
    if not git.is_repo(repo_root):
        profile.reasons.append("git 저장소 아님")
        return profile
    profile.base_branch = str(override.get("base_branch") or git.default_branch(repo_root))
    profile.has_remote = git.has_remote(repo_root)
    profile.deny = tuple(str(d) for d in (override.get("deny") or []))
    profile.deploy = dict(override.get("deploy") or {})
    dirs = _candidate_dirs(repo_root)

    # frontend
    fe_dir = override.get("frontend")
    fe_candidates = [fe_dir] if fe_dir else [d for d in dirs if d.lower() in FE_DIR_NAMES or "front" in d.lower()] + ["."]
    for d in fe_candidates:
        side = classify_frontend(repo_root, d)
        if side:
            profile.frontend = side
            break
    # backend
    be_dir = override.get("backend")
    be_candidates = [be_dir] if be_dir else [d for d in dirs if d.lower() in BE_DIR_NAMES or "back" in d.lower() or "server" in d.lower()] + ["."]
    for d in be_candidates:
        if profile.frontend and d == profile.frontend.dir:
            continue
        side = classify_backend(repo_root, d)
        if side:
            profile.backend = side
            break

    if profile.frontend is None:
        profile.reasons.append("프런트엔드 디렉터리 없음")
    if profile.backend is None:
        profile.reasons.append("백엔드 디렉터리 없음")
    if profile.eligible:
        profile.reasons.append("FE·BE 모두 확인")
        if not profile.has_verification():
            profile.reasons.append("자동 검증 명령 없음(자동 머지 제외)")
    return profile


# ---------- fleet 설정 ----------

APPROVAL_MODES = ("auto", "manual")


def _approval_mode(value: Any, default: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("", "default"):
        return default
    if v not in APPROVAL_MODES:
        raise ValueError(f"approval 모드는 auto|manual 이어야 합니다: {value!r}")
    return v


@dataclass
class FleetConfig:
    root: Path
    active_hours: str | None = "01:00-08:00"
    no_new_work_minutes_before_end: int = 45
    merge_at: str | None = "07:30"
    poll_seconds: int = 600
    endpoint_retry_seconds: int = 60
    calendar: dict[str, Any] = field(default_factory=dict)   # 날짜별 시간대 예외: {'2026-11-09': '13:00-18:00', '2026-11-10': 'off'}
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    repos: dict[str, dict[str, Any]] = field(default_factory=dict)
    discover: dict[str, Any] = field(default_factory=lambda: {"max_proposals": 2, "max_open_proposed": 4})
    auto_merge: dict[str, Any] = field(default_factory=lambda: {"verify": True, "require_verification": True, "verify_timeout": 900, "push": True, "delete_merged_branches": True})
    approval: dict[str, Any] = field(default_factory=lambda: {"default": "auto", "repos": {}})   # 사람 승인 모드 (기본 + 레포별 예외)
    source: Path | None = None          # 읽어 온 fleet.yaml 경로 (핫리로드용)
    source_mtime: float | None = None

    @classmethod
    def load(cls, path: Path) -> "FleetConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw_root = str(data.get("root") or "").strip()
        if not raw_root:
            root = path.resolve().parent.parent                       # 비우면 hermes-fleet 의 상위 폴더 (어디에 클론해도 형제 레포를 스캔)
        else:
            root = Path(raw_root).expanduser()
            if not root.is_absolute():
                root = (path.resolve().parent / root).resolve()         # 상대 경로는 fleet.yaml 위치 기준 (CWD 가 다른 컨테이너에서도 동일)
        cfg = cls(root=root, source=path, source_mtime=path.stat().st_mtime)
        for key in ("active_hours", "no_new_work_minutes_before_end", "merge_at", "poll_seconds", "endpoint_retry_seconds", "include", "exclude"):
            if key in data:
                setattr(cfg, key, data[key])
        cfg.repos = data.get("repos") or {}
        cfg.calendar = {str(k): ("off" if v is None or v is False else str(v)) for k, v in (data.get("calendar") or {}).items()}
        cfg.discover = {**cfg.discover, **(data.get("discover") or {})}
        cfg.auto_merge = {**cfg.auto_merge, **(data.get("auto_merge") or {})}
        ap = data.get("approval") or {}
        if isinstance(ap, str):
            ap = {"default": ap}
        cfg.approval = {"default": _approval_mode(ap.get("default"), "auto"), "repos": {str(k): _approval_mode(v, "auto") for k, v in (ap.get("repos") or {}).items()}}
        return cfg

    RELOADABLE = ("include", "exclude", "repos", "active_hours", "calendar", "merge_at", "no_new_work_minutes_before_end", "poll_seconds", "endpoint_retry_seconds", "discover", "auto_merge", "approval")

    def approval_for(self, name: str) -> str:
        """레포의 사람 승인 모드: repos 예외 → default."""
        return self.approval.get("repos", {}).get(name) or self.approval.get("default") or "auto"

    def refresh(self) -> list[str]:
        """fleet.yaml 이 바뀌었으면 다시 읽어 바뀐 키 목록을 돌려준다(데몬이 돌고 있는 동안 웹/CLI 의 설정 변경을 반영). root 는 바꾸지 않는다."""
        if self.source is None:
            return []
        try:
            mtime = self.source.stat().st_mtime
            if mtime == self.source_mtime:
                return []
            fresh = FleetConfig.load(self.source)
        except (OSError, ValueError, yaml.YAMLError):
            return []
        changed = [k for k in self.RELOADABLE if getattr(self, k) != getattr(fresh, k)]
        for k in changed:
            setattr(self, k, getattr(fresh, k))
        self.source_mtime = mtime
        return changed

    def refresh_selection(self) -> bool:   # 하위 호환
        return bool(self.refresh())


def scan_repos(cfg: FleetConfig) -> list[RepoProfile]:
    """root 아래 1단계 디렉터리를 모두 프로파일링한다(선정 여부와 사유 포함)."""
    profiles: list[RepoProfile] = []
    if not cfg.root.is_dir():
        return profiles
    for child in sorted(cfg.root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in (cfg.exclude or []):
            profiles.append(RepoProfile(name=child.name, root=child, reasons=["exclude 목록"]))
            continue
        if cfg.include and child.name not in cfg.include:
            continue
        if not git.is_repo(child):
            continue
        prof = detect_profile(child, cfg.repos.get(child.name))
        prof.approval = cfg.approval_for(child.name)
        profiles.append(prof)
    return profiles


def eligible_repos(cfg: FleetConfig) -> list[RepoProfile]:
    return [p for p in scan_repos(cfg) if p.eligible]
