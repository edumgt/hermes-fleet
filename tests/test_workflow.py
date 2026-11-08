"""전달 체인 탐지(workflow.py), fleet 배포 훅, deploy 요청, 워크플로 API 테스트."""
from pathlib import Path

import pytest
from conftest import commit_all, g
from test_fleet import make_daemon

from hermes_fleet import requests as rq
from hermes_fleet import workflow as wf
from hermes_fleet.fleet import load_state
from hermes_fleet.merge import MergeConfig, auto_merge
from hermes_fleet.repos import detect_profile

CD = """name: CD — Deploy to EC2
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
      - run: aws ecr get-login-password | docker login
      - uses: appleboy/ssh-action@v1
        with: {host: ec2-1-2-3-4.compute.amazonaws.com}
"""
CI = "name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"
PAGES = "name: Pages\non:\n  push:\n    branches: [release]\njobs:\n  pages:\n    steps:\n      - uses: actions/deploy-pages@v4\n"


def test_parse_remote():
    assert wf.parse_remote("https://github.com/edumgt/domain-rag-lab.git") == ("github", "https://github.com/edumgt/domain-rag-lab", "edumgt", "domain-rag-lab")
    assert wf.parse_remote("git@github.com:edumgt/hermes-fleet.git") == ("github", "https://github.com/edumgt/hermes-fleet", "edumgt", "hermes-fleet")
    assert wf.parse_remote("https://gitlab.com/a/b")[0] == "gitlab" and wf.parse_remote(None)[0] == "none" and wf.parse_remote("weird")[0] == "other"


def test_detect_delivery_modes(node_python_repo: Path):
    root = node_python_repo
    d = wf.detect_delivery(root, "main")
    assert d.remote_host == "none" and d.push_mode == "none" and d.cicd_mode == "none" and d.deploy_mode == "none" and d.summary == "티켓 → 크루 → 머지"

    g(root, "remote", "add", "origin", "https://github.com/acme/shop.git")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(CI, encoding="utf-8")
    d = wf.detect_delivery(root, "main")
    assert d.push_mode == "auto" and d.cicd_mode == "auto" and d.deploy_mode == "none" and d.owner == "acme" and d.repo == "shop"
    assert d.workflows[0].on_base_push and not d.workflows[0].deployish

    (root / ".github" / "workflows" / "cd.yml").write_text(CD, encoding="utf-8")
    d = wf.detect_delivery(root, "main")
    assert d.deploy_mode == "actions" and d.aws and set(d.targets) >= {"aws-ec2", "aws-ecr"} and "aws-ec2" in d.deploy_target
    assert "Actions" in d.summary and "push 시 자동" in d.summary

    # release 브랜치에만 반응하는 배포 워크플로는 main 기준 자동 배포가 아님
    (root / ".github" / "workflows" / "cd.yml").write_text(PAGES, encoding="utf-8")
    d = wf.detect_delivery(root, "main")
    assert d.deploy_mode == "manual" and d.workflows[0].push_branches == ["release"] and not d.workflows[0].on_base_push
    assert wf.detect_delivery(root, "release").deploy_mode == "actions"

    # 파일 마커: 스크립트의 s3 sync → manual, aws
    (root / ".github" / "workflows" / "cd.yml").unlink()
    (root / "deploy.sh").write_text("aws s3 sync dist/ s3://bucket --delete\naws cloudfront create-invalidation --distribution-id X --paths '/*'\n", encoding="utf-8")
    d = wf.detect_delivery(root, "main")
    assert d.deploy_mode == "manual" and d.aws and {"aws-s3", "aws-cloudfront"} <= set(d.targets) and any(m["path"] == "deploy.sh" for m in d.markers)
    # 이름이 배포성이 아닌 스크립트(데모/유틸)에 적힌 aws 명령은 세지 않는다
    (root / "deploy.sh").unlink()
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "demo_fixture.py").write_text("CMD = 'aws s3 sync . s3://x'\n", encoding="utf-8")
    d = wf.detect_delivery(root, "main")
    assert d.deploy_mode == "none" and not d.aws and not any(m["path"].startswith("scripts/") for m in d.markers)
    (root / "scripts" / "publish-site.sh").write_text("aws s3 sync . s3://x\n", encoding="utf-8")
    d = wf.detect_delivery(root, "main")
    assert d.deploy_mode == "manual" and d.aws
    (root / "scripts" / "publish-site.sh").unlink(); (root / "scripts" / "demo_fixture.py").unlink()
    (root / "deploy.sh").write_text("aws s3 sync dist/ s3://bucket --delete\n", encoding="utf-8")

    # fleet.yaml 선언이 우선
    d = wf.detect_delivery(root, "main", {"target": "aws-s3-cloudfront", "trigger": "fleet", "command": "echo deploy"})
    assert d.deploy_mode == "fleet" and d.deploy_target == "aws-s3-cloudfront" and "fleet 이 실행" in d.summary
    assert wf.detect_delivery(root, "main", {"trigger": "none"}).deploy_mode == "none"
    assert wf.detect_delivery(root, "main", {"target": "aws-s3-cloudfront", "trigger": "manual"}).deploy_mode == "manual"


def test_run_deploy_and_merge_hook(tmp_path: Path, node_python_repo: Path, monkeypatch):
    root = node_python_repo
    ok, detail = wf.run_deploy(root, {"command": "echo deployed-$PWD"}, log=lambda m: None)
    assert ok and "deployed-" in detail
    ok, detail = wf.run_deploy(root, {"command": "exit 3"}, log=lambda m: None)
    assert not ok and detail.startswith("exit 3")
    assert wf.run_deploy(root, {}, log=lambda m: None)[0] is False

    # 머지 → push 성공 → trigger: fleet 이면 deploy.command 실행
    bare = tmp_path / "origin.git"
    g(tmp_path, "init", "-q", "--bare", str(bare))
    g(root, "remote", "add", "origin", str(bare))
    g(root, "push", "-q", "-u", "origin", "main")
    p = detect_profile(root, {"deploy": {"target": "aws-s3-cloudfront", "trigger": "fleet", "command": "echo deploy-ran > deployed.txt"}})
    p.base_branch = "main"
    from hermes_fleet.backlog import write_ticket
    write_ticket(p.backlog_dir, "HX-001", "t", "P1", "backend", ["pm", "dev", "qa"], ["a"], "b", status="todo")
    commit_all(root, "ticket")
    g(root, "push", "-q", "origin", "main")
    g(root, "checkout", "-q", "-b", "hermes/HX-001")
    for f in p.backlog_dir.glob("HX-001-*.md"):
        f.write_text(f.read_text(encoding="utf-8").replace("status: todo", "status: review"), encoding="utf-8")
    commit_all(root, "work")
    g(root, "checkout", "-q", "main")
    logs = []
    report = auto_merge(p, MergeConfig(push=True, verify=True, require_verification=True), log=logs.append, verify=lambda r, lg: (True, "ok"))
    assert report.merged and report.pushed and report.deploy and report.deploy["ok"] and report.deploy["target"] == "aws-s3-cloudfront"
    assert (root / "deployed.txt").read_text().strip() == "deploy-ran" and any("✓ deployed" in m for m in logs)
    assert "deploy: ok aws-s3-cloudfront" in (p.workspace_dir / "merge-report.md").read_text(encoding="utf-8")

    # deploy 요청 (호스트 러너)
    import datetime as dt
    dm, dlogs = make_daemon(tmp_path, [p], dt.datetime(2026, 11, 8, 14, 0), monkeypatch, discover=False)
    (root / "deployed.txt").unlink()
    g(root, "add", "-A"); g(root, "commit", "-q", "-m", "clean")
    rq.create_request(dm.state_dir, "shop", "deploy")
    assert dm.tick() == "work"
    done = rq.list_done(dm.state_dir)[0]
    assert done["status"] == "done" and load_state(dm.repo_state_path("shop"))["last_deploy"]["ok"] and load_state(dm.repo_state_path("shop"))["last_deploy"]["source"] == "request"
    p2 = detect_profile(root)
    p2.base_branch = "main"
    dm2, _ = make_daemon(tmp_path / "b", [p2], dt.datetime(2026, 11, 8, 14, 0), monkeypatch, discover=False)
    g(root, "add", "-A"); g(root, "commit", "-q", "-m", "clean2")
    rq.create_request(dm2.state_dir, "shop", "deploy")
    assert dm2.tick() == "work" and rq.list_done(dm2.state_dir)[0]["status"] == "skipped"


def test_workflow_api(tmp_path: Path, node_python_repo: Path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from test_monitor import STOPPED_LOG, collector, make_fleet

    from hermes_fleet.monitor.app import create_app

    root = node_python_repo
    g(root, "remote", "add", "origin", "https://github.com/acme/shop.git")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "cd.yml").write_text(CD, encoding="utf-8")
    commit_all(root, "cd")
    paths = make_fleet(tmp_path, root, STOPPED_LOG)
    col = collector(paths)
    monkeypatch.setattr(wf, "github_runs", lambda owner, repo, branch, token=None, **kw: {"ok": True, "runs": [{"name": "CD — Deploy to EC2", "status": "completed", "conclusion": "success", "url": "u", "updated_at": "2026-11-08T01:00:00Z", "sha": "abc", "event": "push"}], "web": "w"})
    monkeypatch.setattr("hermes_fleet.monitor.collect.github_runs", wf.github_runs)
    client = TestClient(create_app(col))
    b = client.get("/api/workflow?ci=true").json()
    row = next(r for r in b["rows"] if r["name"] == "shop")
    keys = [s["key"] for s in row["stages"]]
    assert keys == ["discover", "approve", "crew", "review", "merge", "push", "cicd", "deploy"] and row["reach"] == "deploy" and row["aws"]
    modes = {s["key"]: s["mode"] for s in row["stages"]}
    assert modes["push"] == "auto" and modes["cicd"] == "auto" and modes["deploy"] == "actions" and modes["approve"] == "manual"
    dep = row["stages"][7]
    assert dep["status"] == "ok" and "CD — Deploy to EC2" in dep["detail"] and dep["can_request"] is False
    assert b["totals"] == {"targets": 1, "push": 1, "cicd": 1, "deploy_auto": 1, "aws": 1}
    det = client.get("/api/repos/shop").json()
    assert det["delivery"]["deploy_mode"] == "actions" and det["ci"]["ok"] and len(det["workflow"]) == 8
    # command 없는 레포에는 deploy 요청 불가
    assert client.post("/api/repos/shop/requests", json={"action": "deploy"}).status_code == 400
