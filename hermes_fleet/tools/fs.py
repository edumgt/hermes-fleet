"""저장소 루트로 샌드박싱된 파일 시스템 도구. root 는 항상 명시한다(레포마다 다르다)."""
from __future__ import annotations

import re
from pathlib import Path

DENY_PREFIXES = (".git/", "node_modules/", "hermes/.venv/", ".venv/", "venv/", "target/", "dist/")
DENY_NAMES = {".env"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "target", "__pycache__", ".pytest_cache", "build"}
MAX_READ_CHARS = 60_000


class SandboxError(PermissionError):
    pass


def _denied(rel: str, deny: tuple[str, ...]) -> bool:
    return any(rel == d.rstrip("/") or rel.startswith(d if d.endswith("/") else d + "/") for d in deny)


def resolve_in_repo(path: str, root: Path, deny: tuple[str, ...] = ()) -> Path:
    """레포 안의 경로로 해석. deny 는 레포별 추가 금지 경로(접두사; 'state/' 처럼 디렉터리는 / 로 끝냄, 파일은 그대로)."""
    candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    try:
        rel = candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SandboxError(f"path escapes repository: {path}") from exc
    rel_str = rel.as_posix()
    if any(rel_str == p.rstrip("/") or rel_str.startswith(p if p.endswith("/") else p + "/") or rel_str == p for p in (*DENY_PREFIXES, *deny)):
        raise SandboxError(f"path is denied: {rel_str}")
    if candidate.name in DENY_NAMES or (candidate.name.startswith(".env") and candidate.name != ".env.example"):
        raise SandboxError(f"path is denied: {rel_str}")
    return candidate


def read_file(path: str, start_line: int = 1, max_lines: int = 400, root: Path = None, deny: tuple[str, ...] = ()) -> str:
    target = resolve_in_repo(path, root, deny)
    if not target.is_file():
        return f"ERROR: not a file: {path}"
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(start_line, 1)
    chunk = lines[start - 1 : start - 1 + max_lines]
    body = "\n".join(f"{i:5d}| {line}" for i, line in enumerate(chunk, start=start))
    footer = ""
    if start - 1 + max_lines < len(lines):
        footer = f"\n... ({len(lines)} lines total, continue with start_line={start + max_lines})"
    return (body + footer)[:MAX_READ_CHARS]


def write_file(path: str, content: str, root: Path = None, deny: tuple[str, ...] = ()) -> str:
    target = resolve_in_repo(path, root, deny)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"OK: wrote {len(content)} chars to {target.relative_to(root.resolve()).as_posix()}"


def list_dir(path: str = ".", depth: int = 2, root: Path = None, deny: tuple[str, ...] = ()) -> str:
    base = resolve_in_repo(path, root, deny)
    if not base.is_dir():
        return f"ERROR: not a directory: {path}"
    out: list[str] = []

    def walk(d: Path, level: int) -> None:
        if level > depth:
            return
        for child in sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if child.name in SKIP_DIRS:
                continue
            rel = child.relative_to(root.resolve()).as_posix()
            if _denied(rel, deny):
                continue
            out.append(f"{'  ' * (level - 1)}{rel}{'/' if child.is_dir() else ''}")
            if child.is_dir():
                walk(child, level + 1)

    walk(base, 1)
    return "\n".join(out) if out else "(empty)"


def search_text(pattern: str, path: str = ".", max_results: int = 50, root: Path = None, deny: tuple[str, ...] = ()) -> str:
    base = resolve_in_repo(path, root, deny)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: invalid regex: {exc}"
    results: list[str] = []
    files = [base] if base.is_file() else base.rglob("*")
    for file in files:
        if not file.is_file() or any(part in SKIP_DIRS for part in file.parts) or _denied(file.relative_to(root.resolve()).as_posix(), deny):
            continue
        if file.suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".lock", ".mp4", ".jar", ".class"}:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = file.relative_to(root.resolve()).as_posix()
                results.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(results) >= max_results:
                    return "\n".join(results) + f"\n... (truncated at {max_results})"
    return "\n".join(results) if results else "(no matches)"
