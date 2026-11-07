import datetime as dt
from pathlib import Path

from conftest import commit_all, g

from hermes_fleet import fleet as f
from hermes_fleet.backlog import Ticket, write_ticket
from hermes_fleet.repos import FleetConfig, detect_profile
from hermes_fleet.settings import Settings


def mk(tid: str, priority: str, status: str = "todo") -> Ticket:
    return Ticket.from_markdown(f"---\nid: {tid}\ntitle: t {tid}\npriority: {priority}\nstatus: {status}\narea: docs\nstages: [pm, dev, qa]\nacceptance:\n  - a\n---\nbody\n")


def test_pick_next_and_rotate():
    tickets = [mk("HX-003", "P1"), mk("HX-002", "P0"), mk("HX-001", "P0"), mk("HX-004", "P0", "proposed")]
    assert f.pick_next(tickets, {"tickets": {}}).id == "HX-001"
    assert f.pick_next(tickets, {"tickets": {"HX-001": {}, "HX-002": {}, "HX-003": {}}}) is None

    class R:  # noqa: D401
        def __init__(self, n): self.name = n
    items = [R("a"), R("b"), R("c")]
    assert [r.name for r in f.rotate(items, "a")] == ["b", "c", "a"]
    assert [r.name for r in f.rotate(items, "c")] == ["a", "b", "c"]
    assert [r.name for r in f.rotate(items, None)] == ["a", "b", "c"]


def make_daemon(tmp_path: Path, profiles, now: dt.datetime, monkeypatch, **opts):
    monkeypatch.setattr(f, "check_endpoint", lambda s: {"ok": True})
    logs = []
    cfg = FleetConfig(root=tmp_path, active_hours="01:00-08:00", merge_at="07:30")
    dm = f.FleetDaemon(cfg, f.DaemonOptions(once=True, **opts), settings=Settings(), state_dir=tmp_path / "state", pause_file=tmp_path / "state" / "PAUSE",
                       log=logs.append, sleep=lambda s: None, repos=lambda c: profiles, now=lambda: now)
    return dm, logs


def test_tick_stops_outside_window_and_waits_near_end(tmp_path: Path, node_python_repo: Path, monkeypatch):
    p = detect_profile(node_python_repo)
    dm, _ = make_daemon(tmp_path, [p], dt.datetime(2026, 11, 8, 9, 0), monkeypatch)
    assert dm.tick() == "stop"
    dm, _ = make_daemon(tmp_path, [p], dt.datetime(2026, 11, 8, 9, 0), monkeypatch, exit_outside_window=False)
    assert dm.tick() == "wait"
    dm, logs = make_daemon(tmp_path, [p], dt.datetime(2026, 11, 8, 7, 40), monkeypatch)
    monkeypatch.setattr(dm, "_merge_due", lambda p, now: False)
    assert dm.tick() == "wait" and any("window end" in m for m in logs)


def test_tick_skips_repo_on_foreign_branch(tmp_path: Path, node_python_repo: Path, monkeypatch):
    root = node_python_repo
    g(root, "checkout", "-q", "-b", "feature/human-work")
    p = detect_profile(root)
    p.base_branch = "main"
    write_ticket(p.backlog_dir, "HX-001", "x", "P1", "backend", ["pm", "dev", "qa"], ["a"], "b", status="todo")
    commit_all(root, "ticket")
    dm, logs = make_daemon(tmp_path, [p], dt.datetime(2026, 11, 8, 2, 0), monkeypatch, discover=False)
    ran = []
    monkeypatch.setattr(dm, "_run_ticket", lambda p, t, s: ran.append(t.id))
    assert dm.tick() == "wait" and ran == [] and any("leaving it for a human" in m for m in logs)
    assert g(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature/human-work"


def test_tick_round_robin_and_discover(tmp_path: Path, node_python_repo: Path, monkeypatch):
    from conftest import git_init
    a = detect_profile(node_python_repo)
    b_root = tmp_path / "second"
    git_init(b_root)
    (b_root / "frontend").mkdir(); (b_root / "frontend" / "index.html").write_text("<html/>")
    (b_root / "backend").mkdir(); (b_root / "backend" / "main.py").write_text("x=1")
    commit_all(b_root)
    b = detect_profile(b_root)
    assert a.eligible and b.eligible
    write_ticket(a.backlog_dir, "HX-001", "x", "P1", "backend", ["pm", "dev", "qa"], ["a"], "b", status="todo")
    commit_all(a.root, "ticket")
    write_ticket(b.backlog_dir, "HX-001", "y", "P1", "backend", ["pm", "dev", "qa"], ["a"], "b", status="todo")
    commit_all(b.root, "ticket")

    now = dt.datetime(2026, 11, 8, 2, 0)
    dm, _ = make_daemon(tmp_path, [a, b], now, monkeypatch)
    ran = []
    monkeypatch.setattr(dm, "_run_ticket", lambda p, t, s: ran.append((p.name, t.id)) or s.setdefault("tickets", {}).__setitem__(t.id, {}) or f.save_state(s, dm.repo_state_path(p.name)))
    assert dm.tick() == "work" and ran == [("shop", "HX-001")]
    assert dm.tick() == "work" and ran[-1] == ("second", "HX-001")
    discovered = []
    monkeypatch.setattr(dm, "_run_discover", lambda p: discovered.append(p.name))
    assert dm.tick() == "work" and discovered == ["shop"]      # 커서가 second 였으므로 shop 부터
    assert dm.tick() == "work" and discovered == ["shop", "second"]


def test_merge_due_once_per_day(tmp_path: Path, node_python_repo: Path, monkeypatch):
    p = detect_profile(node_python_repo)
    dm, logs = make_daemon(tmp_path, [p], dt.datetime(2026, 11, 8, 7, 35), monkeypatch, discover=False)
    merged = []
    monkeypatch.setattr(f, "auto_merge", lambda prof, cfg, log: merged.append(prof.name) or type("R", (), {"merged": [], "pushed": False, "note": "", "outcomes": [], "started_at": "t"})())
    assert dm.tick() == "work" and merged == ["shop"]
    assert dm._merge_due(p, dt.datetime(2026, 11, 8, 7, 50)) is False
    assert dm._merge_due(p, dt.datetime(2026, 11, 9, 7, 50)) is True


def test_scaffold_is_idempotent(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    created = f.ensure_scaffold(p)
    assert {c.name for c in created} == {".gitignore", "README.md", ".gitkeep"}
    assert f.ensure_scaffold(p) == []
