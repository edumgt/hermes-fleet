"""읽기 전용 git 도구 + 파이프라인/데몬이 쓰는 브랜치·커밋 헬퍼. root 필수."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(args: list[str], root: Path, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)  # noqa: S603,S607


def git_status(root: Path) -> str:
    proc = _git(["status", "--short", "--branch"], root)
    return proc.stdout or proc.stderr or "(clean)"


def git_diff(path: str = "", root: Path = None, max_chars: int = 20_000) -> str:
    args = ["diff", "--stat", "--patch"]
    if path:
        args += ["--", path]
    proc = _git(args, root)
    out = proc.stdout or "(no changes)"
    return out[:max_chars] + ("\n... (truncated)" if len(out) > max_chars else "")


def is_repo(root: Path) -> bool:
    """git 이 실제로 다룰 수 있는 저장소인가 (.git 존재 + 소유권/손상 문제 없음)."""
    if not (root / ".git").exists():
        return False
    return _git(["rev-parse", "--is-inside-work-tree"], root).returncode == 0


def is_clean(root: Path) -> bool:
    return _git(["status", "--porcelain"], root).stdout.strip() == ""


def current_branch(root: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()


def default_branch(root: Path) -> str:
    """origin/HEAD → main/master 존재 여부 → 현재 브랜치 순으로 기준 브랜치를 추정."""
    ref = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], root).stdout.strip()
    if ref:
        return ref.rsplit("/", 1)[-1]
    for name in ("main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], root).returncode == 0:
            return name
    return current_branch(root) or "main"


def has_remote(root: Path, name: str = "origin") -> bool:
    return name in _git(["remote"], root).stdout.split()


def ensure_branch(name: str, root: Path) -> str:
    exists = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], root).returncode == 0
    _git(["checkout", name] if exists else ["checkout", "-b", name], root, check=True)
    return name


def checkout(name: str, root: Path) -> None:
    _git(["checkout", name], root, check=True)


def commit_all(message: str, root: Path) -> str:
    _git(["add", "-A"], root, check=True)
    if is_clean(root):
        return "(nothing to commit)"
    proc = _git(["commit", "-m", message], root)
    return proc.stdout.strip() or proc.stderr.strip()
