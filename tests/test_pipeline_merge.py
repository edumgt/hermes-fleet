from pathlib import Path

import pytest
from conftest import commit_all, g

from hermes_fleet import prompts
from hermes_fleet.backlog import Ticket
from hermes_fleet.merge import MergeConfig, auto_merge, list_ticket_branches, ticket_on_branch
from hermes_fleet.org import load_org_for, load_template
from hermes_fleet.pipeline import Pipeline, excerpt
from hermes_fleet.repos import detect_profile
from hermes_fleet.settings import Settings

TICKET = "---\nid: {id}\ntitle: 티켓 {id}\npriority: P2\nstatus: {status}\narea: backend\nstages: [pm, dev, qa]\nacceptance:\n  - a\n---\nbody\n"


def test_org_template_and_profile_project(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    org = load_org_for(p)
    assert org["project"]["name"] == "shop" and "frontend (node)" in org["project"]["description"]
    known = {"read_file", "write_file", "list_dir", "search_text", "run_command", "git_status", "git_diff"}
    for role in load_template()["roles"].values():
        assert set(role["tools"]) <= known


def test_repo_org_override_wins(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    override = p.org_override
    override.parent.mkdir(parents=True)
    org = load_template()
    org["project"] = {"name": "custom", "description": "d", "conventions": []}
    import yaml
    override.write_text(yaml.safe_dump(org, allow_unicode=True))
    assert load_org_for(p)["project"]["name"] == "custom"


def test_pipeline_plan_and_verdict(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    ticket = Ticket.from_markdown(TICKET.format(id="HX-900", status="todo"))
    pipe = Pipeline(p, ticket, settings=Settings(), max_iterations=3, log=lambda _: None)
    plan = pipe.plan()
    assert [s["stage"] for s in plan] == ["pm", "dev", "qa", "review-loop"]
    assert plan[0]["output"] == "hermes/workspace/HX-900/prd.md"
    assert pipe.parse_verdict("x\nVERDICT: PASS") == "PASS" and pipe.parse_verdict("VERDICT: PASS\n...\nVERDICT: FAIL") == "FAIL"
    assert excerpt("x" * 50, 10).startswith("x" * 10) and "truncated" in excerpt("x" * 50, 10)
    with pytest.raises(ValueError):
        Pipeline(p, ticket, settings=Settings(), stages=["deploy"])


def test_prompts_include_profile(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    text = prompts.dev_task("T", "PRD", "hermes/workspace/X/prd.md", None, None, "QA said FAIL", 2, p.profile_text())
    assert "npm test" in text and "--with-requirements" in text and "QA 재작업" in text
    assert "VERDICT: PASS" in prompts.qa_task("T", "P", "p", "D", "d", 1, p.profile_text())


def _branch(root: Path, tid: str, status: str, content: str) -> None:
    g(root, "checkout", "-q", "-b", f"hermes/{tid}")
    (root / "hermes" / "backlog").mkdir(parents=True, exist_ok=True)
    (root / f"{tid}.txt").write_text(content)
    (root / "hermes" / "backlog" / f"{tid}-x.md").write_text(TICKET.format(id=tid, status=status))
    commit_all(root, f"work {tid}")
    g(root, "checkout", "-q", "main")


def test_merge_review_branch_with_verify_stub(node_python_repo: Path):
    root = node_python_repo
    _branch(root, "HX-001", "review", "a")
    _branch(root, "HX-002", "blocked", "b")
    p = detect_profile(root)
    assert list_ticket_branches(root) == [("hermes/HX-001", "HX-001"), ("hermes/HX-002", "HX-002")]
    assert ticket_on_branch(root, "hermes/HX-001", "HX-001").status == "review"
    calls = []
    report = auto_merge(p, MergeConfig(push=False), log=lambda m: None, verify=lambda r, lg: (calls.append(1) or (True, "ok")))
    results = {o.ticket_id: o.result for o in report.outcomes}
    assert results == {"HX-001": "merged", "HX-002": "skipped"} and calls == [1]
    assert (root / "HX-001.txt").exists() and "status: done" in (root / "hermes/backlog/HX-001-x.md").read_text()
    assert list_ticket_branches(root) == [("hermes/HX-002", "HX-002")]


def test_merge_reverts_on_verify_failure(node_python_repo: Path):
    root = node_python_repo
    _branch(root, "HX-001", "review", "a")
    head = g(root, "rev-parse", "HEAD").strip()
    report = auto_merge(detect_profile(root), MergeConfig(push=False), log=lambda m: None, verify=lambda r, lg: (False, "tests failed"))
    assert report.outcomes[0].result == "verify-failed"
    assert g(root, "rev-parse", "HEAD").strip() == head and not (root / "HX-001.txt").exists()


def test_merge_requires_verification_capability(tmp_path: Path):
    from conftest import git_init
    root = tmp_path / "static"
    git_init(root)
    (root / "fe").mkdir(); (root / "fe" / "index.html").write_text("<html/>")
    (root / "pom.xml").write_text("<project/>"); (root / "src" / "main").mkdir(parents=True)
    commit_all(root)
    p = detect_profile(root)
    p.backend.test_cmd = None  # mvn 없는 환경을 흉내
    report = auto_merge(p, MergeConfig(push=False), log=lambda m: None)
    assert "auto-merge disabled" in report.note and report.outcomes == []
