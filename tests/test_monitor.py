"""웹 모니터 수집기/API 테스트 (LLM 불필요). 임시 fleet 디렉터리 + node_python_repo 픽스처로 상태·로그·티켓을 만든다."""
import datetime as dt
import json
from pathlib import Path

import pytest
from conftest import commit_all, g

from hermes_fleet.backlog import write_ticket
from hermes_fleet.monitor import collect as c

NOW = dt.datetime(2026, 11, 8, 3, 0, 0)


def make_fleet(tmp_path: Path, repo: Path, log_lines: list[str] | None = None, log_age_seconds: int = 0) -> c.MonitorPaths:
    fleet = tmp_path / "fleet"
    (fleet / "state").mkdir(parents=True)
    (fleet / "logs").mkdir()
    (fleet / "fleet.yaml").write_text(f"root: {tmp_path}\nactive_hours: '01:00-08:00'\nmerge_at: '07:30'\npoll_seconds: 600\nexclude: [fleet]\n", encoding="utf-8")
    (fleet / "state" / "fleet.json").write_text(json.dumps({"cursor": repo.name, "last_run": {"repo": repo.name, "at": "2026-11-08T02:40:00"}}), encoding="utf-8")
    (fleet / "state" / f"{repo.name}.json").write_text(json.dumps({
        "tickets": {"HX-001": {"branch": "hermes/HX-001", "started_at": "2026-11-08T02:00:00", "finished_at": "2026-11-08T02:40:00",
                               "verdict": "PASS", "iterations": 1, "status": "review", "commit": "[hermes/HX-001 abc] hermes(HX-001): x", "seconds": 2400}},
        "last_merge_date": "2026-11-07",
        "last_merge": {"at": "2026-11-07 07:30:01", "merged": ["HX-000"], "pushed": False, "note": "", "issues": []},
    }), encoding="utf-8")
    if log_lines is not None:
        log = fleet / "logs" / "daemon.log"
        log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        import os
        ts = dt.datetime.now().timestamp() - log_age_seconds
        os.utime(log, (ts, ts))
    return c.MonitorPaths(fleet_dir=fleet, config=fleet / "fleet.yaml")


RUNNING_LOG = [
    "2026-11-08 01:00:02 [fleet] start root=/x hours=01:00-08:00 merge_at=07:30 model=ollama/hermes3:8b @ http://h:11434",
    "2026-11-08 01:00:03 [fleet][shop] ▶ HX-001 add tests [P1]",
    "2026-11-08 01:00:04 [shop][pm] iteration 1 → hermes/workspace/HX-001/prd.md",
    "some crewai verbose line without timestamp",
    "2026-11-08 01:05:00 [shop][dev] iteration 1 → hermes/workspace/HX-001/dev-report.md",
]
STOPPED_LOG = RUNNING_LOG + [
    "2026-11-08 02:40:00 [fleet][shop] ■ HX-001 → review on hermes/HX-001 (2400s)",
    "2026-11-08 08:00:01 [fleet] outside active hours; exiting (cron will start again in the window)",
]


def collector(paths: c.MonitorPaths, now: dt.datetime = NOW) -> c.Collector:
    col = c.Collector(paths, now=lambda: now)
    col._llm_cache = (float("inf"), {"ok": False, "provider": "ollama", "model": "m", "base_url": "u", "url": "u/v1/models", "error": "test", "models": [],
                                     "role_models": {}, "missing_models": [], "checked_at": "x"})  # 네트워크 호출 방지
    return col


def test_schedule_and_daemon_running(tmp_path: Path, node_python_repo: Path):
    paths = make_fleet(tmp_path, node_python_repo, RUNNING_LOG)
    col = collector(paths)
    o = col.overview()
    s = o["schedule"]
    assert s["in_window"] and s["can_start_work"] and s["minutes_until_end"] == 300 and s["next_window_start"] is None
    d = o["daemon"]
    assert d["status"] == "running" and d["method"] == "log" and d["expected_running"]
    assert d["activity"]["kind"] == "ticket" and d["activity"]["id"] == "HX-001" and d["activity"]["repo"] == "shop"
    assert d["activity"]["stage"] == {"name": "dev", "iteration": 1, "output": "hermes/workspace/HX-001/dev-report.md", "since": "2026-11-08 01:05:00"}
    assert o["cursor"] == "shop" and o["eligible_count"] == 1 and o["monitor"]["in_docker"] in (True, False)


def test_daemon_stopped_stale_paused_and_next_window(tmp_path: Path, node_python_repo: Path):
    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    col = collector(paths, now=dt.datetime(2026, 11, 8, 9, 0))
    o = col.overview()
    assert o["daemon"]["status"] == "stopped" and o["daemon"]["activity"] is None and not o["daemon"]["expected_running"]
    assert o["schedule"]["in_window"] is False and o["schedule"]["next_window_start"] == "2026-11-09T01:00:00"

    paths = make_fleet(tmp_path / "b", node_python_repo, RUNNING_LOG, log_age_seconds=4 * 3600)
    col = collector(paths)
    d = col.overview()["daemon"]
    assert d["status"] == "stale" and d["activity"]["note"]

    paths.pause_file.write_text("paused at now\n", encoding="utf-8")
    d = col.overview()["daemon"]
    assert d["paused"] and d["pause_note"] == "paused at now" and not d["expected_running"]


def test_daemon_without_log(tmp_path: Path, node_python_repo: Path):
    paths = make_fleet(tmp_path, node_python_repo, None)
    d = collector(paths).overview()["daemon"]
    assert d["status"] == "stopped" and d["log_exists"] is False and d["expected_running"] is True


def test_repos_tickets_and_state_override(tmp_path: Path, node_python_repo: Path):
    root = node_python_repo
    backlog = root / "hermes" / "backlog"
    write_ticket(backlog, "HX-001", "add tests", "P1", "backend", ["pm", "dev", "qa"], ["a"], "body", status="todo")
    write_ticket(backlog, "HX-002", "proposal: x", "P2", "frontend", ["pm", "dev", "qa"], ["a"], "why", status="proposed")
    (root / "hermes" / ".gitignore").write_text("workspace/*/logs/\n", encoding="utf-8")   # 스캐폴드(발굴/ init 이 main 에 커밋)
    commit_all(root, "tickets")
    # HX-001 은 hermes/HX-001 브랜치에서 처리되어 산출물과 review 상태가 브랜치에만 있다
    g(root, "checkout", "-q", "-b", "hermes/HX-001")
    ws = root / "hermes" / "workspace" / "HX-001"
    ws.mkdir(parents=True)
    (ws / "qa-report.md").write_text("ok\n\nVERDICT: PASS\n", encoding="utf-8")
    (ws / "summary.md").write_text("# Run Summary\n", encoding="utf-8")
    (ws / "logs").mkdir()
    (ws / "logs" / "run.jsonl").write_text('{"ts":"2026-11-08T02:10:00","stage":"qa","iteration":1,"seconds":30.2,"output":"x","chars":10}\n', encoding="utf-8")
    t = backlog / "HX-001-add-tests.md"
    t.write_text(t.read_text(encoding="utf-8").replace("status: todo", "status: review"), encoding="utf-8")
    commit_all(root, "hermes(HX-001)")
    g(root, "checkout", "-q", "main")
    assert not (ws / "qa-report.md").exists() and (ws / "logs" / "run.jsonl").exists()

    paths = make_fleet(tmp_path, root, STOPPED_LOG)
    col = collector(paths)
    repos = col.repos()
    shop = next(r for r in repos if r["name"] == "shop")
    assert shop["eligible"] and shop["on_base"] and shop["clean"] and shop["has_verification"]
    assert shop["counts"]["review"] == 1 and shop["counts"]["proposed"] == 1 and shop["counts"]["todo"] == 0
    assert shop["hermes_branches"] == [{"branch": "hermes/HX-001", "ticket_id": "HX-001"}]
    assert shop["last_merge"]["merged"] == ["HX-000"] and shop["processed_count"] == 1 and shop["last_ticket"]["id"] == "HX-001"
    assert shop["last_commit"]["subject"] == "hermes(HX-001)" or shop["last_commit"]["subject"] == "tickets"

    detail = col.repo_detail("shop")
    ids = {t["id"]: t for t in detail["tickets"]}
    assert ids["HX-001"]["status"] == "review" and ids["HX-001"]["status_source"] == "state" and ids["HX-001"]["file_status"] == "todo"
    assert ids["HX-002"]["status"] == "proposed" and ids["HX-002"]["status_source"] == "file"
    assert col.repo_detail("nope") is None

    td = col.ticket_detail("shop", "hx-001")
    assert td["status"] == "review" and td["branch"] == "hermes/HX-001" and td["branch_status"] == "review"
    names = {a["name"]: a["source"] for a in td["artifacts"]}
    assert names == {"qa-report.md": "branch", "summary.md": "branch"}
    assert td["events"][0]["stage"] == "qa" and "qa-report.md" in (td["diff_stat"] or "")
    art = col.artifact("shop", "HX-001", "qa-report.md")
    assert art["source"] == "branch" and "VERDICT: PASS" in art["content"]
    assert col.artifact("shop", "HX-001", "../fleet.yaml") is None and col.artifact("shop", "HX-001", "nope.md") is None
    assert col.ticket_detail("shop", "HX-999") is None


def test_log_tail_filters_and_stream_offsets(tmp_path: Path, node_python_repo: Path):
    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    col = collector(paths)
    out = col.log_tail(500)
    assert out["exists"] and len(out["lines"]) == len(STOPPED_LOG)
    assert out["lines"][3]["ts"] is None and out["lines"][3]["level"] == "info"
    assert out["lines"][1]["level"] == "start" and out["lines"][1]["repo"] == "shop"
    assert out["lines"][2]["repo"] == "shop" and out["lines"][5]["level"] == "done"
    assert [l["msg"] for l in col.log_tail(500, repo="shop")["lines"]] and all(l["repo"] == "shop" for l in col.log_tail(500, repo="shop")["lines"])
    assert len(col.log_tail(500, query="exiting")["lines"]) == 1
    assert len(col.log_tail(2)["lines"]) == 2

    size = col.log_size()
    assert col.log_read_from(size) == (size, [])
    with paths.daemon_log.open("a", encoding="utf-8") as fh:
        fh.write("2026-11-08 08:00:02 new line\npartial")
    off, new = col.log_read_from(size)
    assert new == ["2026-11-08 08:00:02 new line"] and off == size + len("2026-11-08 08:00:02 new line\n")
    paths.daemon_log.write_text("tiny\n", encoding="utf-8")   # 파일이 줄어들면 처음부터
    assert col.log_read_from(off) == (5, ["tiny"])


def test_timeline_merges_state_and_log(tmp_path: Path, node_python_repo: Path):
    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    ev = collector(paths).timeline(50)
    kinds = [e["kind"] for e in ev]
    assert "ticket" in kinds and "merge" in kinds and "log" in kinds and "cursor" in kinds
    assert ev == sorted(ev, key=lambda e: e["ts"], reverse=True)
    t = next(e for e in ev if e["kind"] == "ticket")
    assert t["id"] == "HX-001" and t["verdict"] == "PASS" and t["repo"] == "shop"


def test_probe_lock(tmp_path: Path):
    import fcntl
    lock = tmp_path / "daemon.lock"
    assert c.probe_lock(lock) is None
    lock.write_text("", encoding="utf-8")
    assert c.probe_lock(lock) is False
    with lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert c.probe_lock(lock) is True


def test_api(tmp_path: Path, node_python_repo: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hermes_fleet.monitor.app import create_app

    write_ticket(node_python_repo / "hermes" / "backlog", "HX-001", "add tests", "P1", "backend", ["pm", "dev", "qa"], ["a"], "body", status="todo")
    commit_all(node_python_repo, "t")
    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    client = TestClient(create_app(collector(paths)))
    assert client.get("/api/health").json()["ok"] is True
    o = client.get("/api/overview").json()
    assert o["eligible_count"] == 1 and o["daemon"]["status"] == "stopped"
    assert "shop" in {r["name"] for r in client.get("/api/repos").json()} and [r["name"] for r in client.get("/api/repos?all=false").json()] == ["shop"]
    assert client.get("/api/repos/shop").json()["tickets"][0]["id"] == "HX-001"
    assert client.get("/api/repos/shop/tickets/HX-001").json()["status"] == "review"
    assert client.get("/api/repos/shop/tickets/HX-404").status_code == 404
    assert client.get("/api/repos/nope").status_code == 404
    assert len(client.get("/api/logs?lines=3").json()["lines"]) == 3
    assert client.get("/api/timeline?limit=5").status_code == 200
    html = client.get("/").text
    assert "Hermes Fleet" in html and client.get("/app.js").status_code == 200 and client.get("/style.css").status_code == 200


# ---------- 레포 선택/해제 (fleet.yaml 편집) ----------

FLEET_YAML = """# 설정
root: /r
include: []
exclude:
  - hermes-fleet
  - legacy-lab      # 사유 주석
repos:
  a: {frontend: fe}
"""


def test_toggle_text_preserves_comments_and_validates():
    from hermes_fleet import fleetfile as ff

    out = ff.toggle_text(FLEET_YAML, "shop", False)
    assert "  - legacy-lab      # 사유 주석\n  - shop\n" in out and out.startswith("# 설정\n") and "repos:\n  a: {frontend: fe}" in out
    assert ff.toggle_text(out, "shop", False) == out                      # 멱등
    back = ff.toggle_text(out, "shop", True)
    assert back == FLEET_YAML                                             # 원복
    sel = ff.toggle_text(FLEET_YAML, "legacy-lab", True)
    assert "legacy-lab" not in sel and "  - hermes-fleet\n" in sel

    # exclude 가 비면 인라인 빈 목록으로, 다시 추가하면 블록으로
    one = "root: /r\nexclude:\n  - x\n"
    assert ff.toggle_text(one, "x", True) == "root: /r\nexclude: []\n"
    assert ff.toggle_text("root: /r\nexclude: []  # none\n", "x", False) == "root: /r\nexclude: # none\n  - x\n"
    assert ff.toggle_text("root: /r\nexclude: [a, b]\n", "b", True) == "root: /r\nexclude:\n  - a\n"
    # include 가 있으면 선택 시 include 에도 추가, 해제 시 include 는 그대로
    inc = "root: /r\ninclude:\n  - a\nexclude: []\n"
    assert ff.toggle_text(inc, "b", True) == "root: /r\ninclude:\n  - a\n  - b\nexclude: []\n"
    assert ff.toggle_text(inc, "a", False) == "root: /r\ninclude:\n  - a\nexclude:\n  - a\n"
    # 키가 없으면 끝에 추가
    assert ff.toggle_text("root: /r\n", "z", False) == "root: /r\n\nexclude:\n  - z\n"
    assert ff.is_selected("a", [], ["a"]) is False and ff.is_selected("a", ["b"], []) is False and ff.is_selected("a", [], []) is True


def test_set_selected_via_collector_and_daemon_hot_reload(tmp_path: Path, node_python_repo: Path, monkeypatch):
    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    col = collector(paths)
    shop = next(r for r in col.repos() if r["name"] == "shop")
    assert shop["selected"] and shop["eligible"] and shop["fe_be_ok"]
    assert all(r["name"] != "fleet" for r in col.repos())              # git 저장소가 아니면 목록에 없음

    res = col.set_selected("shop", False)
    assert res["changed"] and "shop" in res["exclude"] and res["now_selected"] is False
    shop = next(r for r in col.repos() if r["name"] == "shop")
    assert shop["selected"] is False and shop["eligible"] is False and shop["fe_be_ok"] is True and shop["reasons"][0] == "exclude 목록"
    assert shop["counts"] is not None                                   # 제외돼도 FE/BE 가 있으면 내역은 보인다
    o = col.overview()
    assert o["eligible_count"] == 0 and o["exclude"] == ["fleet", "shop"] and o["config_writable"] is True
    assert col.set_selected("shop", True)["now_selected"] is True
    assert col.set_selected("shop", True)["changed"] is False
    with pytest.raises(Exception):
        col.set_selected("nope", False)

    # 데몬: 돌고 있는 동안 fleet.yaml 이 바뀌면 다음 tick 에 include/exclude 를 다시 읽는다
    import time
    from hermes_fleet import fleet as f
    from hermes_fleet.repos import FleetConfig, eligible_repos
    from hermes_fleet.settings import Settings
    cfg = FleetConfig.load(paths.config)
    logs = []
    dm = f.FleetDaemon(cfg, f.DaemonOptions(once=True, discover=False), settings=Settings(), state_dir=paths.state_dir, pause_file=paths.pause_file,
                       log=logs.append, sleep=lambda s: None, repos=eligible_repos, now=lambda: dt.datetime(2026, 11, 8, 9, 0))
    assert [p.name for p in dm._targets()] == ["shop"]
    time.sleep(0.01)
    col.set_selected("shop", False)
    os_utime = __import__("os").utime
    os_utime(paths.config, None)
    assert dm._targets() == [] and any("fleet.yaml changed" in m for m in logs)
    col.set_selected("shop", True)
    os_utime(paths.config, (time.time() + 1, time.time() + 1))
    assert [p.name for p in dm._targets()] == ["shop"]


def test_stats_and_merge_report_parse(tmp_path: Path, node_python_repo: Path):
    ws = node_python_repo / "hermes" / "workspace"
    ws.mkdir(parents=True)
    (ws / "merge-report.md").write_text(
        "## 2026-11-07 07:30:01\n\n- merged         hermes/HX-000 (HX-000) first\n- skipped        hermes/HX-009 (HX-009) status=blocked (needs review)\n- pushed: False\n\n"
        "## 2026-11-08 07:30:02\n\n- note: no runnable verification\n- conflict       hermes/HX-003 (HX-003) CONFLICT\n- pushed: True\n\n", encoding="utf-8")
    (ws / "HX-001" / "logs").mkdir(parents=True)
    (ws / "HX-001" / "logs" / "run.jsonl").write_text('{"ts":"2026-11-08T02:10:00","stage":"pm","iteration":1,"seconds":100}\n{"ts":"2026-11-08T02:30:00","stage":"dev","iteration":1,"seconds":1000.5}\n{"ts":"2026-11-08T02:40:00","stage":"dev","iteration":2,"seconds":500}\n', encoding="utf-8")
    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    col = collector(paths)
    runs = c.parse_merge_report((ws / "merge-report.md").read_text(encoding="utf-8"))
    assert [r["at"] for r in runs] == ["2026-11-08 07:30:02", "2026-11-07 07:30:01"]
    assert runs[1]["merged"] == ["HX-000"] and runs[0]["issues"] == ["HX-003:conflict"] and runs[0]["pushed"] and runs[0]["note"].startswith("no runnable")
    st = col.stats(14)
    assert st["per_repo"][0]["name"] == "shop" and st["per_repo"][0]["merged_total"] == 1 and st["per_repo"][0]["verdicts"]["PASS"] == 1
    assert st["per_repo"][0]["stage_seconds"] == {"pm": 100.0, "design": 0.0, "dev": 1500.5, "qa": 0.0} and st["stage_totals"]["dev"] == 1500.5
    today = next(d for d in st["daily"] if d["date"] == "2026-11-08")
    assert today["PASS"] == 1 and today["merged"] == 0 and today["seconds"] == 2400
    assert next(d for d in st["daily"] if d["date"] == "2026-11-07")["merged"] == 1 and len(st["daily"]) == 14
    detail = col.repo_detail("shop")
    assert detail["history"][0]["id"] == "HX-001" and detail["history"][0]["stage_seconds"]["dev"] == 1500.5
    assert len(detail["merge_history"]) == 2 and isinstance(detail["log_lines"], list) and len(detail["daily"]) == 14
    kinds = [e for e in col.timeline(50) if e["kind"] == "merge"]
    assert len(kinds) == 2


def test_control_api(tmp_path: Path, node_python_repo: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hermes_fleet import requests as rq
    from hermes_fleet.monitor.app import create_app

    write_ticket(node_python_repo / "hermes" / "backlog", "HX-001", "add tests", "P1", "backend", ["pm", "dev", "qa"], ["a"], "body", status="proposed")
    commit_all(node_python_repo, "t")
    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    client = TestClient(create_app(collector(paths)))
    o = client.get("/api/overview").json()
    assert o["control"]["writable"] and o["control"]["pending"] == [] and o["control"]["paused_all"] is False

    r = client.post("/api/repos/shop/requests", json={"action": "approve", "ticket": "hx-001"})
    assert r.status_code == 200 and r.json()["ticket"] == "HX-001"
    assert client.post("/api/repos/shop/requests", json={"action": "bogus"}).status_code == 400
    assert client.post("/api/repos/nope/requests", json={"action": "run"}).status_code == 400
    q = client.get("/api/requests").json()
    assert len(q["pending"]) == 1 and q["pending"][0]["action"] == "approve"
    repo = next(x for x in client.get("/api/overview").json()["repos"] if x["name"] == "shop")
    assert repo["pending_requests"] == ["approve HX-001"] and repo["paused"] is False
    assert client.delete(f"/api/requests/{q['pending'][0]['id']}").status_code == 200 and client.get("/api/requests").json()["pending"] == []
    assert client.delete("/api/requests/nope").status_code == 404

    assert client.put("/api/repos/shop/pause", json={"paused": True}).json()["paused"] is True
    assert rq.is_repo_paused(paths.state_dir, "shop") and client.get("/api/overview").json()["daemon"]["paused_repos"] == ["shop"]
    client.put("/api/repos/shop/pause", json={"paused": False})
    assert client.put("/api/pause", json={"paused": True}).status_code == 200 and paths.pause_file.exists()
    client.put("/api/pause", json={"paused": False})
    assert not paths.pause_file.exists()

    r = client.put("/api/schedule", json={"active_hours": "00:30-07:00", "merge_at": "06:45"})
    assert r.status_code == 200 and r.json()["schedule"]["active_hours"] == "00:30-07:00" and r.json()["schedule"]["merge_at"] == "06:45"
    assert "00:30-07:00" in paths.config.read_text(encoding="utf-8")
    assert client.put("/api/schedule", json={"active_hours": "bad"}).status_code == 400
    assert client.put("/api/schedule", json={}).status_code == 400

    # 제외된 레포에는 run 요청 불가, stop 은 가능
    client.put("/api/repos/shop/selection", json={"selected": False})
    assert client.post("/api/repos/shop/requests", json={"action": "run"}).status_code == 400
    assert client.post("/api/repos/shop/requests", json={"action": "stop"}).status_code == 200


# ---------- 날짜별 시간대(달력) ----------

def test_update_calendar_rewrites_block_and_keeps_rest(tmp_path: Path):
    from hermes_fleet.fleetfile import update_calendar
    from hermes_fleet.repos import FleetConfig
    p = tmp_path / "fleet.yaml"
    p.write_text("# 설정\nroot: /r\nactive_hours: \"01:00-08:00\"   # 기본\nexclude:\n  - a   # 사유\nauto_merge:\n  verify: true\n", encoding="utf-8")
    saved = update_calendar(p, {"2026-11-10": "13:00-18:00", "2026-11-11": "off"})
    assert saved == {"2026-11-10": "13:00-18:00", "2026-11-11": "off"}
    text = p.read_text(encoding="utf-8")
    assert "# 사유" in text and "# 기본" in text and 'calendar:\n  "2026-11-10": "13:00-18:00"\n  "2026-11-11": off\n' in text
    cfg = FleetConfig.load(p)
    assert cfg.calendar == {"2026-11-10": "13:00-18:00", "2026-11-11": "off"}
    # 수정·삭제 → 블록만 바뀐다
    saved = update_calendar(p, {"2026-11-10": None, "2026-11-12": "21:00-03:00"})
    assert saved == {"2026-11-11": "off", "2026-11-12": "21:00-03:00"}
    text2 = p.read_text(encoding="utf-8")
    assert text2.count("calendar:") == 1 and "auto_merge:\n  verify: true" in text2 and "# 사유" in text2
    with pytest.raises(Exception):
        update_calendar(p, {"2026-13-40": "off"})
    with pytest.raises(Exception):
        update_calendar(p, {"2026-11-12": "25:00-26:00"})
    update_calendar(p, {"2026-11-11": None, "2026-11-12": None})
    assert "calendar: {}" in p.read_text(encoding="utf-8") and FleetConfig.load(p).calendar == {}


def test_calendar_api_and_schedule_view(tmp_path: Path, node_python_repo: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hermes_fleet.monitor.app import create_app

    paths = make_fleet(tmp_path, node_python_repo, STOPPED_LOG)
    client = TestClient(create_app(collector(paths, now=dt.datetime(2026, 11, 8, 14, 0))))
    s0 = client.get("/api/overview").json()["schedule"]
    assert s0["in_window"] is False and s0["calendar"] == {} and s0["today"]["kind"] == "default"
    r = client.put("/api/calendar", json={"entries": {"2026-11-08": "13:00-18:00", "2026-11-09": "off"}})
    assert r.status_code == 200
    s1 = r.json()["schedule"]
    assert s1["in_window"] is True and s1["today"] == {"date": "2026-11-08", "kind": "override", "spec": "13:00-18:00"} and s1["minutes_until_end"] == 240
    assert s1["calendar"] == {"2026-11-08": "13:00-18:00", "2026-11-09": "off"}
    d = {x["date"]: x for x in s1["days"]}
    assert d["2026-11-09"]["kind"] == "off" and d["2026-11-10"]["kind"] == "default" and d["2026-11-10"]["spec"] == "01:00-08:00"
    o = client.get("/api/overview").json()
    assert o["daemon"]["expected_running"] is True      # 오늘 예외 창 안인데 데몬이 stopped → 미실행 표시
    assert client.put("/api/calendar", json={"entries": {"2026-11-08": "bad"}}).status_code == 400
    assert client.put("/api/calendar", json={"entries": {"2026-11-08": None, "2026-11-09": None}}).json()["calendar"] == {}



def test_monitor_without_config_reads_parent_and_flags_uninitialized(tmp_path: Path, node_python_repo: Path):
    fleet = tmp_path / "hermes-fleet"
    (fleet / "state").mkdir(parents=True); (fleet / "logs").mkdir()
    (fleet / "fleet.example.yaml").write_text((Path(__file__).resolve().parent.parent / "fleet.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    col = collector(c.MonitorPaths(fleet_dir=fleet, config=fleet / "fleet.yaml"))
    o = col.overview()
    assert o["initialized"] is False and o["config_writable"] is False
    assert node_python_repo.name in {r["name"] for r in o["repos"]}      # 템플릿 기본값으로 상위 폴더를 읽기 전용 스캔
