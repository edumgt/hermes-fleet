"""사람 승인 모드: auto(제안 → 바로 todo, proposed 자동 승인) / manual(proposed 로 두고 사람 승인)."""
import datetime as dt
from pathlib import Path

import pytest
from conftest import commit_all, g

from hermes_fleet import fleet as f
from hermes_fleet.backlog import load_tickets, write_ticket
from hermes_fleet.discover import write_proposals
from hermes_fleet.fleetfile import update_approval
from hermes_fleet.repos import FleetConfig, detect_profile


def test_config_default_and_override(tmp_path: Path):
    y = tmp_path / "fleet.yaml"
    y.write_text("root: /r\napproval:\n  default: auto\n  repos:\n    shop: manual\n", encoding="utf-8")
    cfg = FleetConfig.load(y)
    assert cfg.approval_for("shop") == "manual" and cfg.approval_for("other") == "auto"
    y.write_text("root: /r\n", encoding="utf-8")
    assert FleetConfig.load(y).approval_for("x") == "auto"           # 블록이 없으면 auto
    y.write_text("root: /r\napproval: manual\n", encoding="utf-8")
    assert FleetConfig.load(y).approval_for("x") == "manual"         # 문자열 축약형
    y.write_text("root: /r\napproval:\n  default: sometimes\n", encoding="utf-8")
    with pytest.raises(ValueError):
        FleetConfig.load(y)


def test_update_approval_block_roundtrip(tmp_path: Path):
    y = tmp_path / "fleet.yaml"
    y.write_text("root: /r\nexclude:\n  - a   # 사유\n", encoding="utf-8")
    assert update_approval(y, repos={"shop": "manual"}) == {"default": "auto", "repos": {"shop": "manual"}}
    assert update_approval(y, default="manual", repos={"shop": None, "lab": "auto"}) == {"default": "manual", "repos": {"lab": "auto"}}
    text = y.read_text(encoding="utf-8")
    assert text.count("approval:") == 1 and "# 사유" in text and FleetConfig.load(y).approval_for("lab") == "auto" and FleetConfig.load(y).approval_for("zzz") == "manual"
    with pytest.raises(Exception):
        update_approval(y, default="maybe")


def test_write_proposals_status(tmp_path: Path, node_python_repo: Path):
    d = node_python_repo / "hermes" / "backlog"
    props = [{"title": "로그인 개선", "priority": "P1", "area": "frontend", "stages": ["dev", "qa"], "acceptance": ["a"], "background": "b", "scope": ["s"], "files": []}]
    write_proposals(props, d, log=lambda m: None, repo_root=node_python_repo, status="todo")
    assert [t.status for t in load_tickets(d)] == ["todo"]
    write_proposals([{**props[0], "title": "완전히 다른 제목의 작업"}], d, log=lambda m: None, repo_root=node_python_repo)
    assert sorted(t.status for t in load_tickets(d)) == ["proposed", "todo"]


def test_daemon_auto_approves_proposed_and_discovers_todo(tmp_path: Path, node_python_repo: Path, monkeypatch):
    p = detect_profile(node_python_repo)
    p.approval = "auto"
    write_ticket(p.backlog_dir, "HX-001", "x", "P1", "backend", ["pm", "dev", "qa"], ["a"], "b", status="proposed")
    commit_all(p.root, "proposal")
    monkeypatch.setattr(f, "check_endpoint", lambda s: {"ok": True})
    logs = []
    cfg = FleetConfig(root=tmp_path, active_hours="01:00-08:00", merge_at="07:30")
    dm = f.FleetDaemon(cfg, f.DaemonOptions(once=True, discover=False), settings=__import__("hermes_fleet.settings", fromlist=["Settings"]).Settings(), state_dir=tmp_path / "state",
                       pause_file=tmp_path / "state" / "PAUSE", log=logs.append, sleep=lambda s: None, repos=lambda c: [p], now=lambda: dt.datetime(2026, 11, 8, 2, 0))
    ran = []
    monkeypatch.setattr(dm, "_run_ticket", lambda prof, t, s: ran.append(t.id))
    assert dm.tick() == "work" and ran == ["HX-001"]                 # proposed 가 자동 승인되어 바로 실행된다
    assert load_tickets(p.backlog_dir)[0].status == "todo" and "auto-approve" in g(p.root, "log", "-1", "--pretty=%s")
    assert any("auto-approved 1 proposed" in m for m in logs)
    # manual 이면 그대로 둔다
    p2 = detect_profile(node_python_repo); p2.approval = "manual"
    write_ticket(p2.backlog_dir, "HX-002", "y", "P1", "backend", ["dev", "qa"], ["a"], "b", status="proposed"); commit_all(p2.root, "p2")
    assert dm._auto_approve(p2) == 0 and sorted(t.status for t in load_tickets(p2.backlog_dir)) == ["proposed", "todo"]
    # 발굴: auto 는 todo 로 쓴다
    seen = {}
    monkeypatch.setattr(f, "run_discover", lambda prof, settings, max_proposals, log, status="proposed": seen.setdefault("status", status) and [])
    dm._run_discover(p); assert seen["status"] == "todo"
    seen.clear(); dm._run_discover(p2); assert seen["status"] == "proposed"


def test_api_approval(tmp_path: Path, node_python_repo: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from test_monitor import collector, make_fleet

    from hermes_fleet.monitor.app import create_app

    paths = make_fleet(tmp_path, node_python_repo, [])
    client = TestClient(create_app(collector(paths)))
    shop = next(r for r in client.get("/api/repos").json() if r["name"] == "shop")
    assert shop["approval"] == "auto" and shop["approval_override"] is False
    r = client.put("/api/repos/shop/approval", json={"mode": "manual"}).json()
    assert r["approval"] == "manual" and r["override"] is True
    wf = next(x for x in client.get("/api/workflow").json()["rows"] if x["name"] == "shop")
    assert next(s for s in wf["stages"] if s["key"] == "approve")["mode"] == "manual"
    assert client.put("/api/approval", json={"mode": "manual"}).status_code == 200
    assert client.put("/api/repos/shop/approval", json={"mode": "default"}).json()["approval"] == "manual"   # 예외 삭제 → 기본(manual)
    assert client.put("/api/repos/shop/approval", json={"mode": "weird"}).status_code == 400
    assert client.get("/api/overview").json()["approval"]["default"] == "manual"
