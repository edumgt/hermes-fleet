"""Jev 레포 판정(review): 사실 수집 → 판정 → fleet.yaml 적용 → state/review.json. Jev 는 가짜 decider 또는 가짜 HTTP 로 대체한다."""
import json
from pathlib import Path

from conftest import commit_all, g, git_init

from hermes_fleet import jev
from hermes_fleet import review as rv
from hermes_fleet.backlog import write_ticket
from hermes_fleet.repos import FleetConfig

SRC = Path(__file__).resolve().parent.parent


def make_repo(root: Path, name: str, fe: bool = True, be: bool = True) -> Path:
    r = root / name
    git_init(r)
    if fe:
        (r / "frontend").mkdir(); (r / "frontend" / "index.html").write_text("<html/>")
    if be:
        (r / "backend").mkdir(); (r / "backend" / "main.py").write_text("x=1")
    (r / "README.md").write_text(name)
    commit_all(r)
    return r


def make_fleet(tmp_path: Path, extra: str = "") -> tuple[Path, Path]:
    fleet = tmp_path / "hermes-fleet"
    git_init(fleet)
    (fleet / "state").mkdir(); (fleet / "logs").mkdir()
    (fleet / "fleet.yaml").write_text(f"root: {tmp_path}\ninclude: []\nexclude:\n  - old-lab\nreview:\n  mode: apply\n  min_confidence: 0.6\n{extra}", encoding="utf-8")
    return fleet, fleet / "fleet.yaml"


def answers(choice: str, p: float, conf: float, activity: float = 2.0, changed: float = 0.1, model: str = "jev-1.14.0"):
    return {"model": model, "answers": {
        "proceed": {"type": "choice", "choice": choice, "confidence": conf, "probabilities": {"proceed": p, "skip": round(1 - p, 3)}},
        "activity": {"type": "score", "score": activity, "confidence": 0.8, "legend": {str(i): t for i, t in enumerate(rv.ACTIVITY_LEVELS)}, "probabilities": {}},
        "changed": {"type": "noul", "noul": changed}}, "usage": {"input_tokens": 100, "output_tokens": 5}}


def test_run_review_applies_decisions_with_guardrails(tmp_path: Path):
    fleet, cfg_path = make_fleet(tmp_path, extra="  pinned: [keeper]\n")
    make_repo(tmp_path, "shop")                      # 선택됨, Jev proceed → keep
    make_repo(tmp_path, "sandbox")                   # 선택됨, Jev skip(확신) → exclude
    make_repo(tmp_path, "unsure")                    # 선택됨, Jev skip 이지만 confidence 낮음 → keep
    make_repo(tmp_path, "old-lab")                   # exclude 였는데 Jev proceed → include(되살림)
    make_repo(tmp_path, "api-only", fe=False)        # FE 없음, Jev proceed 라도 keep
    make_repo(tmp_path, "keeper")                    # pinned, Jev skip 이라도 keep
    busy = make_repo(tmp_path, "busy")               # 진행 중 티켓 → Jev skip 이라도 keep
    write_ticket(busy / "hermes" / "backlog", "HX-001", "work", "P2", "backend", ["dev", "qa"], ["x"], "body", status="in-progress")
    commit_all(busy, "ticket")
    plan = {"shop": ("proceed", 0.9, 0.9), "sandbox": ("skip", 0.1, 0.85), "unsure": ("skip", 0.4, 0.3), "old-lab": ("proceed", 0.8, 0.75),
            "api-only": ("proceed", 0.9, 0.9), "keeper": ("skip", 0.1, 0.95), "busy": ("skip", 0.2, 0.9), "hermes-fleet": ("skip", 0.1, 0.99)}
    states = {}

    def fake(state, qs):
        states[state["repository"]] = (state, qs)
        return answers(*plan[state["repository"]])

    res = rv.run_review(fleet, FleetConfig.load(cfg_path), trigger="start", decider=fake)
    assert res["ok"] and res["error"] is None and res["model"] == "jev-1.14.0" and res["trigger"] == "start"
    by = {r["name"]: r for r in res["repos"]}
    assert by["shop"]["decision"] == "keep" and not by["shop"]["applied"]
    assert by["sandbox"]["decision"] == "exclude" and by["sandbox"]["applied"] and by["sandbox"]["selected_after"] is False
    assert by["unsure"]["decision"] == "keep" and "confidence 0.30" in by["unsure"]["reason"]
    assert by["old-lab"]["decision"] == "include" and by["old-lab"]["applied"] and by["old-lab"]["selected_after"] is True
    assert by["api-only"]["decision"] == "keep" and "FE·BE 미탐지" in by["api-only"]["reason"] and "repos.<name>" in by["api-only"]["reason"]
    assert by["keeper"]["decision"] == "keep" and "pinned" in by["keeper"]["reason"]
    assert by["busy"]["decision"] == "keep" and "진행 중" in by["busy"]["reason"]
    assert by["hermes-fleet"]["decision"] == "keep" and "하네스 자신" in by["hermes-fleet"]["reason"]
    assert res["applied"] == 2 and res["usage"]["input_tokens"] == 100 * len(plan)
    cfg = FleetConfig.load(cfg_path)
    assert "sandbox" in cfg.exclude and "old-lab" not in cfg.exclude and cfg.include == []
    # 질문 형식(Jev 계약) 과 state 사실
    st, qs = states["shop"]
    assert set(qs) == {"proceed", "activity", "changed"} and qs["proceed"]["type"] == "choice" and set(qs["proceed"]["criteria"]) == {"proceed", "skip"}
    assert qs["activity"]["type"] == "score" and 2 <= len(qs["activity"]["criteria"]) <= 10 and qs["changed"]["type"] == "noul"
    assert st["frontend"]["dir"] == "frontend" and st["backend"]["stack"] == "python" and st["currently_selected_as_project"] is True
    assert st["changes_since_previous_review"]["status"] == "first_review" and st["git"]["days_since_last_commit"] == 0 and st["git"]["commits_last_30_days"] == 1
    assert states["busy"][0]["has_in_progress_work"] is True and states["api-only"][0]["frontend"] is None
    # state/review.json + 로그
    saved = json.loads((fleet / "state" / "review.json").read_text(encoding="utf-8"))
    assert saved["ok"] and set(saved["heads"]) == set(plan) and len(saved["heads"]["shop"]) == 40
    assert "[review][sandbox] exclude applied" in (fleet / "logs" / "review.log").read_text(encoding="utf-8")


def test_second_review_sees_changes_since_previous_head(tmp_path: Path):
    fleet, cfg_path = make_fleet(tmp_path)
    shop = make_repo(tmp_path, "shop")
    make_repo(tmp_path, "still")
    rv.run_review(fleet, FleetConfig.load(cfg_path), decider=lambda s, q: answers("proceed", 0.9, 0.9))
    (shop / "backend" / "api.py").write_text("y=2"); (shop / "docs").mkdir(); (shop / "docs" / "a.md").write_text("d")
    commit_all(shop, "feat: add api")
    states = {}

    def fake(state, qs):
        states[state["repository"]] = state
        return answers("proceed", 0.9, 0.9)

    rv.run_review(fleet, FleetConfig.load(cfg_path), decider=fake)
    ch = states["shop"]["changes_since_previous_review"]
    assert ch["status"] == "changed" and ch["commit_count"] == 1 and ch["commits"][0].endswith("feat: add api")
    assert ch["changed_top_level_dirs"] == ["backend", "docs"] and ch["changed_files"] == 2
    assert states["still"]["changes_since_previous_review"]["status"] == "unchanged"


def test_advise_and_dry_run_do_not_edit_and_errors_are_recorded(tmp_path: Path):
    fleet, cfg_path = make_fleet(tmp_path, extra="  mode: advise\n")
    make_repo(tmp_path, "sandbox")
    res = rv.run_review(fleet, FleetConfig.load(cfg_path), decider=lambda s, q: answers("skip", 0.1, 0.9))
    by = {r["name"]: r for r in res["repos"]}
    assert res["ok"] and by["sandbox"]["decision"] == "exclude" and not by["sandbox"]["applied"] and res["applied"] == 0
    assert "sandbox" not in FleetConfig.load(cfg_path).exclude
    # dry-run (apply 모드여도 수정 없음)
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace("mode: advise", "mode: apply"), encoding="utf-8")
    res = rv.run_review(fleet, FleetConfig.load(cfg_path), decider=lambda s, q: answers("skip", 0.1, 0.9), dry_run=True)
    assert res["mode"] == "dry-run" and res["applied"] == 0 and "sandbox" not in FleetConfig.load(cfg_path).exclude
    # Jev 오류 → ok False, 이전 heads 유지, fleet.yaml 그대로
    def boom(s, q):
        raise jev.JevError("Jev HTTP 401 API 키가 없거나 잘못됨: bad", status=401)
    res = rv.run_review(fleet, FleetConfig.load(cfg_path), decider=boom)
    assert not res["ok"] and res["error_status"] == 401 and "401" in res["error"]
    saved = json.loads((fleet / "state" / "review.json").read_text(encoding="utf-8"))
    assert not saved["ok"] and "sandbox" in saved["heads"]
    # 키 없음(실제 Jev 경로) → 호출 없이 안내만
    import os
    os.environ["TYPESAFE_API_KEY"] = ""
    res = rv.run_review(fleet, FleetConfig.load(cfg_path))
    assert not res["ok"] and "TYPESAFE_API_KEY" in res["error"] and res["repos"] == []


def test_on_start_flag_and_config_validation(tmp_path: Path, monkeypatch):
    fleet, cfg_path = make_fleet(tmp_path)
    cfg = FleetConfig.load(cfg_path)
    assert cfg.review["mode"] == "apply" and cfg.review["on_start"] is True and cfg.review["min_confidence"] == 0.6
    monkeypatch.setenv("HERMES_REVIEW", "auto"); assert rv.on_start_enabled(cfg)
    monkeypatch.setenv("HERMES_REVIEW", "skip"); assert not rv.on_start_enabled(cfg)
    monkeypatch.delenv("HERMES_REVIEW")
    cfg.review["on_start"] = False; assert not rv.on_start_enabled(cfg)
    import pytest
    with pytest.raises(ValueError, match="review.mode"):
        rv.ReviewSettings.from_config({"mode": "yolo"})
    cfg_path.write_text(f"root: {tmp_path}\nreview: false\n", encoding="utf-8")
    assert FleetConfig.load(cfg_path).review["on_start"] is False


def test_monitor_api_review_runs_through_fake_jev_http(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from hermes_fleet.monitor import collect as c
    from hermes_fleet.monitor.app import create_app

    fleet, cfg_path = make_fleet(tmp_path)
    make_repo(tmp_path, "sandbox")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(jev, "_http", lambda req, t: (200, json.dumps(answers("skip", 0.1, 0.9)).encode(), {}))
    col = c.Collector(c.MonitorPaths(fleet_dir=fleet, config=cfg_path))
    col._llm_cache = (float("inf"), {"ok": False, "provider": "ollama", "model": "m", "base_url": "u", "url": "u", "error": "test", "models": [], "role_models": {}, "missing_models": [], "checked_at": "x"})
    client = TestClient(create_app(col))
    st = client.get("/api/review").json()
    assert st["last"] is None and st["key_configured"] and st["mode"] == "apply" and not st["running"]
    res = client.post("/api/review", json={"dry_run": False, "trigger": "web"}).json()
    assert res["ok"] and res["applied"] == 1 and {r["name"]: r["decision"] for r in res["repos"]} == {"hermes-fleet": "keep", "sandbox": "exclude"}
    assert "sandbox" in FleetConfig.load(cfg_path).exclude
    repos = {r["name"]: r for r in client.get("/api/repos").json()}
    assert repos["sandbox"]["review"]["decision"] == "exclude" and repos["sandbox"]["review"]["jev"]["choice"] == "skip" and repos["sandbox"]["excluded"]
    o = client.get("/api/overview").json()
    assert o["review"]["last"]["applied"] == 1 and o["review"]["model"] == "jev-latest"
    # 기동 이벤트: HERMES_REVIEW=skip 이면 스레드를 만들지 않는다
    monkeypatch.setenv("HERMES_REVIEW", "skip")
    with TestClient(create_app(col)):
        assert not col.review_running()
