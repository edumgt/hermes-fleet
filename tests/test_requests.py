"""요청 큐·레포 일시정지·on-demand 데몬·중단·스케줄 편집·샌드박스 deny 테스트 (LLM 불필요)."""
import datetime as dt
import time
from pathlib import Path

import pytest
from conftest import commit_all, g
from test_fleet import make_daemon

from hermes_fleet import fleetfile as ff
from hermes_fleet import requests as rq
from hermes_fleet.backlog import find_ticket, write_ticket
from hermes_fleet.fleet import load_state, save_state
from hermes_fleet.pipeline import Pipeline, PipelineAborted
from hermes_fleet.repos import FleetConfig, detect_profile
from hermes_fleet.settings import FLEET_DIR
from hermes_fleet.tools import fs

NIGHT = dt.datetime(2026, 11, 8, 2, 0)
DAY = dt.datetime(2026, 11, 8, 14, 0)


def test_request_queue_lifecycle(tmp_path: Path):
    st = tmp_path / "state"
    r = rq.create_request(st, "shop", "run")
    assert r["status"] == "pending" and r["repo"] == "shop" and r["id"].endswith("-shop-run")
    assert rq.create_request(st, "shop", "run")["id"] == r["id"]                       # 같은 요청 중복 방지
    r2 = rq.create_request(st, "shop", "approve", "hx-001")
    assert r2["ticket"] == "HX-001" and len(rq.list_pending(st)) == 2
    with pytest.raises(ValueError):
        rq.create_request(st, "shop", "approve")
    with pytest.raises(ValueError):
        rq.create_request(st, "shop", "bogus")
    assert rq.next_pending(st)["id"] == r["id"]
    rq.mark_running(st, r)
    assert rq.list_pending(st)[0]["status"] == "running" and rq.next_pending(st)["id"] == r2["id"]
    done = rq.complete(st, r, "done", "ok")
    assert done["status"] == "done" and len(rq.list_pending(st)) == 1 and rq.list_done(st)[0]["id"] == r["id"]
    assert rq.cancel(st, r2["id"]) and rq.list_pending(st) == [] and rq.list_done(st)[0]["status"] == "skipped"
    assert rq.cancel(st, "nope") is False
    for i in range(rq.KEEP_DONE + 5):
        rq.complete(st, rq.create_request(st, f"r{i}", "merge"), "done")
    assert len(list(rq.done_dir(st).glob("*.json"))) == rq.KEEP_DONE

    assert rq.paused_repos(st) == [] and not rq.is_repo_paused(st, "shop")
    rq.set_repo_paused(st, "shop", True)
    assert rq.is_repo_paused(st, "shop") and rq.paused_repos(st) == ["shop"]
    rq.set_repo_paused(st, "shop", False)
    assert not rq.is_repo_paused(st, "shop")


def _repo_with_tickets(root: Path):
    p = detect_profile(root)
    p.base_branch = "main"
    write_ticket(p.backlog_dir, "HX-001", "todo one", "P1", "backend", ["pm", "dev", "qa"], ["a"], "b", status="todo")
    write_ticket(p.backlog_dir, "HX-002", "proposal", "P2", "backend", ["pm", "dev", "qa"], ["a"], "b", status="proposed")
    (root / "hermes" / ".gitignore").write_text("workspace/*/logs/\n", encoding="utf-8")
    commit_all(root, "tickets")
    return p


def test_requests_are_handled_first_even_outside_window(tmp_path: Path, node_python_repo: Path, monkeypatch):
    p = _repo_with_tickets(node_python_repo)
    dm, logs = make_daemon(tmp_path, [p], DAY, monkeypatch, discover=False)
    ran = []

    def fake_run(profile, ticket, state):
        ran.append(ticket.id)
        state.setdefault("tickets", {})[ticket.id] = {"verdict": "PASS", "status": "review", "seconds": 1}
        save_state(state, dm.repo_state_path(profile.name))
    monkeypatch.setattr(dm, "_run_ticket", fake_run)

    assert dm.tick() == "stop"                                     # 요청 없음 + 시간대 밖 → 종료
    rq.create_request(dm.state_dir, "shop", "run")
    assert dm.tick() == "work" and ran == ["HX-001"]
    done = rq.list_done(dm.state_dir)[0]
    assert done["status"] == "done" and "HX-001 → review" in done["detail"] and rq.list_pending(dm.state_dir) == []
    assert any("▶ request run" in m for m in logs) and load_state(dm.fleet_state_path())["cursor"] == "shop"

    rq.create_request(dm.state_dir, "shop", "run")                 # 더 이상 todo 없음 → skipped
    assert dm.tick() == "work" and rq.list_done(dm.state_dir)[0]["status"] == "skipped"
    find_ticket("HX-001", p.backlog_dir).set_status("review"); commit_all(node_python_repo, "review")
    rq.create_request(dm.state_dir, "shop", "run", "HX-001")      # 지정 티켓이 todo 가 아님 → skipped
    assert dm.tick() == "work" and "needs todo" in rq.list_done(dm.state_dir)[0]["detail"]
    rq.create_request(dm.state_dir, "other", "merge")              # 대상 아님 → failed
    assert dm.tick() == "work" and rq.list_done(dm.state_dir)[0]["status"] == "failed"


def test_approve_retry_stop_and_pause(tmp_path: Path, node_python_repo: Path, monkeypatch):
    p = _repo_with_tickets(node_python_repo)
    dm, logs = make_daemon(tmp_path, [p], DAY, monkeypatch, discover=False)
    rq.create_request(dm.state_dir, "shop", "approve", "HX-002")
    assert dm.tick() == "work" and rq.list_done(dm.state_dir)[0]["status"] == "done"
    assert find_ticket("HX-002", p.backlog_dir).status == "todo" and "approve → todo" in g(node_python_repo, "log", "-1", "--format=%s")
    rq.create_request(dm.state_dir, "shop", "approve", "HX-002")   # 이미 todo → skipped
    assert dm.tick() == "work" and rq.list_done(dm.state_dir)[0]["status"] == "skipped"

    # retry: blocked 기록 + 파일 상태 blocked → todo, state 기록 삭제
    t = find_ticket("HX-001", p.backlog_dir)
    t.set_status("blocked"); commit_all(node_python_repo, "blocked")
    save_state({"tickets": {"HX-001": {"verdict": "FAIL", "status": "blocked"}}}, dm.repo_state_path("shop"))
    rq.create_request(dm.state_dir, "shop", "retry", "HX-001")
    assert dm.tick() == "work" and rq.list_done(dm.state_dir)[0]["status"] == "done"
    assert find_ticket("HX-001", p.backlog_dir).status == "todo" and "HX-001" not in load_state(dm.repo_state_path("shop"))["tickets"]

    # stop (실행 중인 것 없음) → 레포 일시정지, 이후 야간 선택과 요청 모두 건너뜀
    rq.create_request(dm.state_dir, "shop", "stop")
    assert dm.tick() == "work" and rq.is_repo_paused(dm.state_dir, "shop")
    dm_night, logs2 = make_daemon(tmp_path, [p], NIGHT, monkeypatch, discover=False)
    assert dm_night._targets() == [] and any("paused" in m for m in logs2)
    rq.create_request(dm.state_dir, "shop", "run")
    assert dm.tick() == "work" and rq.list_done(dm.state_dir)[0]["status"] == "skipped" and "paused" in rq.list_done(dm.state_dir)[0]["detail"]
    rq.set_repo_paused(dm.state_dir, "shop", False)
    assert [x.name for x in dm_night._targets()] == ["shop"]

    # 작업 트리가 더러우면 요청도 사람 몫으로 남긴다
    (node_python_repo / "dirty.txt").write_text("x")
    rq.create_request(dm.state_dir, "shop", "merge")
    assert dm.tick() == "work" and "dirty" in rq.list_done(dm.state_dir)[0]["detail"]


def test_on_demand_mode_is_quiet_without_requests(tmp_path: Path, node_python_repo: Path, monkeypatch):
    p = _repo_with_tickets(node_python_repo)
    dm, logs = make_daemon(tmp_path, [p], DAY, monkeypatch, discover=False, on_demand=True)
    assert dm.run() == 0 and logs == []
    monkeypatch.setattr(dm, "_run_merge", lambda prof: type("R", (), {"merged": [], "pushed": False, "note": "n"})())
    rq.create_request(dm.state_dir, "shop", "merge")
    rq.create_request(dm.state_dir, "shop", "stop")
    assert dm.run() == 0
    assert logs[0].endswith("exiting") is False and "on-demand start" in logs[0] and "on-demand done" in logs[-1]
    assert rq.list_pending(dm.state_dir) == [] and len(rq.list_done(dm.state_dir)) == 2
    # 전체 PAUSE 면 요청이 있어도 손대지 않는다
    dm.pause_file.parent.mkdir(exist_ok=True); dm.pause_file.write_text("p")
    rq.create_request(dm.state_dir, "shop", "merge")
    n = len(logs)
    assert dm.run() == 0 and len(logs) == n and len(rq.list_pending(dm.state_dir)) == 1


def test_pipeline_abort_and_daemon_records_aborted(tmp_path: Path, node_python_repo: Path, monkeypatch):
    p = _repo_with_tickets(node_python_repo)
    ticket = find_ticket("HX-001", p.backlog_dir)
    pipe = Pipeline(p, ticket, settings=None, stages=["pm", "dev", "qa"], log=lambda m: None, should_abort=lambda: True)
    with pytest.raises(PipelineAborted):
        pipe.run(create_branch=True, commit=False)
    g(node_python_repo, "checkout", "-q", "main")
    g(node_python_repo, "checkout", "-q", "."); g(node_python_repo, "clean", "-fdq")
    g(node_python_repo, "branch", "-D", "hermes/HX-001")

    dm, logs = make_daemon(tmp_path, [p], NIGHT, monkeypatch, discover=False)
    rq.create_request(dm.state_dir, "shop", "stop")                # 실행 도중 stop 요청이 들어온 상황을 흉내
    monkeypatch.setattr("hermes_fleet.fleet.Pipeline", lambda *a, **k: type("P", (), {"run": lambda self, **kw: (_ for _ in ()).throw(PipelineAborted("aborted before stage pm")) if k["should_abort"]() else None})())
    state = load_state(dm.repo_state_path("shop"))
    dm._run_ticket(p, ticket, state)
    entry = load_state(dm.repo_state_path("shop"))["tickets"]["HX-001"]
    assert entry["verdict"] == "ABORTED" and entry["status"] == "blocked" and rq.is_repo_paused(dm.state_dir, "shop")
    assert rq.list_done(dm.state_dir)[0]["status"] == "done" and any("⏹" in m for m in logs)
    assert g(node_python_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_schedule_edit_and_daemon_hot_reload(tmp_path: Path, node_python_repo: Path, monkeypatch):
    y = tmp_path / "fleet.yaml"
    y.write_text('root: /r\nactive_hours: "01:00-08:00"   # 야간\nmerge_at: "07:30"\npoll_seconds: 600\nexclude: []\n', encoding="utf-8")
    out = ff.update_schedule(y, active_hours="22:00-06:30", merge_at="6:05", no_new_work_minutes_before_end=30)
    assert out["changed"] and out["active_hours"] == "22:00-06:30" and out["merge_at"] == "06:05" and out["no_new_work_minutes_before_end"] == 30
    text = y.read_text(encoding="utf-8")
    assert 'active_hours: "22:00-06:30"   # 야간\n' in text and text.endswith("no_new_work_minutes_before_end: 30\n")
    assert ff.update_schedule(y, merge_at="")["merge_at"] == "" and 'merge_at: ""' in y.read_text(encoding="utf-8")
    with pytest.raises(ff.FleetFileError):
        ff.update_schedule(y, poll_seconds="abc")
    with pytest.raises(ValueError):
        ff.update_schedule(y, active_hours="25:00-01:00")
    with pytest.raises(ff.FleetFileError):
        ff.update_schedule(y, root="/x")

    cfg = FleetConfig.load(y)
    assert cfg.active_hours == "22:00-06:30" and cfg.refresh() == []
    time.sleep(0.01)
    ff.update_schedule(y, active_hours="01:00-09:00", poll_seconds=120)
    import os
    os.utime(y, (time.time() + 2, time.time() + 2))
    assert sorted(cfg.refresh()) == ["active_hours", "poll_seconds"] and cfg.poll_seconds == 120

    p = detect_profile(node_python_repo)
    dm, logs = make_daemon(tmp_path, [p], dt.datetime(2026, 11, 8, 8, 30), monkeypatch)   # 08:30 — 기본 창 밖
    dm.cfg = FleetConfig.load(y)
    dm.window = None
    ff.update_schedule(y, active_hours="01:00-09:30")
    os.utime(y, (time.time() + 4, time.time() + 4))
    dm._refresh_config()
    assert dm.window == (dt.time(1, 0), dt.time(9, 30)) and any("fleet.yaml changed" in m for m in logs)


def test_sandbox_deny_paths(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "state").mkdir(parents=True); (root / "state" / "x.json").write_text("{}")
    (root / "src").mkdir(); (root / "src" / "a.py").write_text("x = 1\n")
    (root / "fleet.yaml").write_text("root: .\n")
    deny = ("state/", "fleet.yaml")
    assert "x = 1" in fs.read_file("src/a.py", root=root, deny=deny)
    for bad in ("state/x.json", "state", "fleet.yaml"):
        with pytest.raises(fs.SandboxError):
            fs.read_file(bad, root=root, deny=deny)
    with pytest.raises(fs.SandboxError):
        fs.write_file("state/new.json", "{}", root=root, deny=deny)
    listing = fs.list_dir(".", 2, root=root, deny=deny)
    assert "src/" in listing and "state" not in listing and "fleet.yaml" not in listing
    assert "state/x.json" not in fs.search_text("x", ".", root=root, deny=deny) and "src/a.py" in fs.search_text("x = 1", ".", root=root, deny=deny)
    assert "fleet.yaml" not in fs.read_file("fleet.yaml", root=root) or True         # deny 없이는 허용(다른 레포)


def test_hermes_fleet_itself_is_a_target():
    override = {"frontend": "hermes_fleet/monitor/static", "backend": ".", "deny": ["state/", "logs/", "fleet.yaml"]}
    p = detect_profile(FLEET_DIR, override)
    assert p.eligible and p.frontend.kind == "static" and p.backend.kind == "python" and p.backend.test_cmd[:2] == ["uv", "run"]
    assert p.deny == ("state/", "logs/", "fleet.yaml") and p.has_verification()
    cfg = FleetConfig.load(FLEET_DIR / "fleet.yaml")
    assert "hermes-fleet" not in cfg.exclude and cfg.repos["hermes-fleet"]["frontend"] == "hermes_fleet/monitor/static"
