from pathlib import Path

import pytest

from hermes_fleet.backlog import Ticket, TicketError, load_tickets, next_ticket_id, write_ticket
from hermes_fleet.tools import fs, shell


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("line1\nconst x = 1\nline3\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_read_write_sandbox(repo: Path):
    assert "    2| const x = 1" in fs.read_file("src/a.js", root=repo)
    assert fs.write_file("src/deep/b.js", "ok", root=repo).startswith("OK")
    for bad in ("../outside.txt", ".env", "node_modules/x.js", ".git/config", "target/x.class"):
        with pytest.raises(fs.SandboxError):
            fs.resolve_in_repo(bad, repo)
    assert fs.resolve_in_repo(".env.example", repo)


@pytest.mark.parametrize("cmd,ok", [
    ("npm test", True), ("npm run build", True), ("pytest -q backend/tests", True),
    ("uv run --with pytest python -m pytest -q", True), ("mvn -q -B test", True), ("./mvnw test", True),
    ("rm -rf /", False), ("npm test && rm x", False), ("curl http://x", False), ("pip install x", False), ("uv run rm -rf x", False),
])
def test_shell_allow_list(cmd: str, ok: bool):
    assert shell.is_allowed(cmd) is ok


def test_shell_runs_in_repo(repo: Path):
    out = shell.run_command("ls src", root=repo)
    assert "a.js" in out and "exit code 0" in out
    assert shell.run_command("ls", cwd="../", root=repo).startswith("ERROR")


def test_ticket_roundtrip_and_status(tmp_path: Path):
    path = write_ticket(tmp_path, "HX-001", "제목: 콜론 포함", "P1", "backend", ["pm", "dev", "qa"], ["조건 A"], "본문", status="proposed")
    t = load_tickets(tmp_path)[0]
    assert t.id == "HX-001" and t.status == "proposed" and t.area == "backend"
    t.set_status("todo")
    assert load_tickets(tmp_path)[0].status == "todo" and path.read_text().count("status:") == 1
    assert next_ticket_id(tmp_path) == "HX-002"
    with pytest.raises(TicketError):
        Ticket.from_markdown("---\nid: X\ntitle: t\nstages: [dev]\nacceptance: [a]\n---\n")
    assert load_tickets(tmp_path / "missing") == []
