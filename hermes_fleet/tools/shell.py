"""허용 목록 기반 셸 실행 도구. 저장소 내부에서, 제한된 명령만 실행한다.

Node(npm/npx/node), Python(pytest/uv run), Java(mvn/mvnw) 테스트·빌드 명령과 읽기 전용 git/파일 명령만 허용.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .fs import SandboxError, resolve_in_repo

ALLOWED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("npm", "test"), ("npm", "run", "test"), ("npm", "run", "build"), ("npm", "run", "lint"),
    ("npm", "ci"), ("npm", "install"),
    ("npx", "vitest"), ("npx", "vite", "build"), ("npx", "jest"),
    ("node", "--test"), ("node", "--check"), ("node", "-e"),
    ("pytest",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"),
    ("python3", "-m", "py_compile"), ("python", "-m", "py_compile"),
    ("uv", "run"), ("uv", "sync"),
    ("mvn",), ("./mvnw",),
    ("git", "status"), ("git", "diff"), ("git", "log"), ("git", "show"),
    ("ls",), ("cat",), ("wc",), ("grep",), ("head",), ("tail",),
)
FORBIDDEN_TOKENS = {"rm", "sudo", "curl", "wget", "ssh", "scp", "docker", "kubectl", "aws", "pip", ">", ">>", "|", "&&", ";", "`", "$("}
MAX_OUTPUT_CHARS = 12_000


def is_allowed(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or any(tok in FORBIDDEN_TOKENS for tok in tokens):
        return False
    return any(tuple(tokens[: len(prefix)]) == prefix for prefix in ALLOWED_COMMANDS)


def run_command(command: str, cwd: str = ".", timeout: int = 900, root: Path = None) -> str:
    if not is_allowed(command):
        return "ERROR: command not allowed. Allowed prefixes: " + ", ".join(" ".join(p) for p in ALLOWED_COMMANDS)
    try:
        workdir = resolve_in_repo(cwd, root)
    except SandboxError as exc:
        return f"ERROR: {exc}"
    if not workdir.is_dir():
        return f"ERROR: cwd is not a directory: {cwd}"
    env = {**os.environ, "CI": "1", "FORCE_COLOR": "0", "NO_COLOR": "1", "MAVEN_OPTS": os.environ.get("MAVEN_OPTS", "-Djansi.passthrough=false")}
    try:
        proc = subprocess.run(shlex.split(command), cwd=workdir, capture_output=True, text=True, timeout=timeout, env=env, check=False)  # noqa: S603
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s: {command}"
    except FileNotFoundError as exc:
        return f"ERROR: {exc}"
    output = f"$ {command}\n(exit code {proc.returncode})\n"
    if proc.stdout:
        output += "--- stdout ---\n" + proc.stdout
    if proc.stderr:
        output += "--- stderr ---\n" + proc.stderr
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[: MAX_OUTPUT_CHARS // 2] + "\n... (truncated) ...\n" + output[-MAX_OUTPUT_CHARS // 2 :]
    return output
