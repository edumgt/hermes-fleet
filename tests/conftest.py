import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def g(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def git_init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    g(root, "init", "-q", "-b", "main")
    g(root, "config", "user.email", "t@t")
    g(root, "config", "user.name", "t")


def commit_all(root: Path, msg: str = "base") -> None:
    g(root, "add", "-A")
    g(root, "commit", "-q", "-m", msg)


@pytest.fixture()
def node_python_repo(tmp_path: Path) -> Path:
    """frontend/(node, vitest) + backend/(python, pytest) 레포."""
    root = tmp_path / "shop"
    git_init(root)
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text('{"name":"fe","scripts":{"test":"vitest run","build":"vite build"},"devDependencies":{"vite":"5"}}')
    (root / "frontend" / "package-lock.json").write_text("{}")
    (root / "frontend" / "src").mkdir()
    (root / "frontend" / "src" / "app.js").write_text("export const a = 1\n")
    (root / "backend").mkdir()
    (root / "backend" / "requirements.txt").write_text("fastapi\n")
    (root / "backend" / "app").mkdir()
    (root / "backend" / "app" / "main.py").write_text("def f():\n    try:\n        pass\n    except Exception:\n        pass\n")
    (root / "backend" / "tests").mkdir()
    (root / "backend" / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    commit_all(root)
    return root
