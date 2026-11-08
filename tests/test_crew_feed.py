"""크루 룸 피드 파서 + /api/crew."""
from pathlib import Path

import pytest
from conftest import commit_all

from hermes_fleet.backlog import write_ticket
from hermes_fleet.monitor.crew import CrewFeedParser, roles_from_org
from hermes_fleet.org import load_template

BOX = lambda title, *lines: [f"╭───── {title} ─────╮", *[f"│  {ln}" + " " * 4 + "│" for ln in lines], "╰──────────────╯"]  # noqa: E731

LOG = [
    "2026-11-08 02:00:00 [fleet] start root=/x hours=01:00-08:00 merge_at=07:30 model=ollama/hermes3:8b @ http://h:11434",
    "2026-11-08 02:00:01 [fleet][shop] ▶ HX-001 add tests [P1]",
    "2026-11-08 02:00:02 [shop][pm] iteration 1 → hermes/workspace/HX-001/prd.md",
    *BOX("🤖 Agent Started", "", "Agent: Product Manager", "", "Task:", "당신은 PM 입니다"),
    *BOX("🔧 Tool Execution Started (#1)", "Agent: Product Manager", "Tool: read_file", "Tool Args: {'path': 'README.md'}"),
    *BOX("✅ Tool Execution Completed (#1)", "Tool: read_file"),
    *BOX("✅ Agent Final Answer", "", "Agent: Product Manager", "", "Final Answer:", "# PRD", "- 목표: 테스트 추가", ""),
    "The context window of model 'x' is unknown, so CrewAI assumes 8,192 tokens",
    "2026-11-08 02:10:00 [shop][dev] iteration 1 → hermes/workspace/HX-001/dev-report.md",
    *BOX("🤖 Agent Started", "Agent: Senior Full-Stack Developer"),
    *BOX("❌ LLM Error", "LLM Call Failed", "Error: connection reset"),
    *BOX("✅ Agent Final Answer", "Agent: Senior Full-Stack Developer", "Final Answer:", "구현 완료"),
    "2026-11-08 02:20:00 [shop][qa] iteration 1 → hermes/workspace/HX-001/qa-report.md",
    *BOX("✅ Agent Final Answer", "Agent: QA Automation Engineer", "Final Answer:", "테스트 통과", "VERDICT: PASS"),
    "2026-11-08 02:25:00 [qa] iteration 1 verdict: PASS",
    "2026-11-08 02:25:01 [fleet][shop] ■ HX-001 → review on hermes/HX-001 (1501s)",
    "2026-11-08 02:25:02 [fleet][shop] ▶ discover (Tech Lead)",
    *BOX("✅ Agent Final Answer", "Agent: Tech Lead", "Final Answer:", "- title: 로그인 개선"),
    "2026-11-08 02:26:00 [discover] proposed HX-002-login.md",
    "2026-11-08 02:26:01 [fleet][shop] ■ proposed 1 tickets",
]


def test_parser_builds_role_conversation():
    p = CrewFeedParser()
    msgs = p.feed(LOG)
    kinds = [(m["role"], m["kind"]) for m in msgs]
    assert kinds[:3] == [("system", "info"), ("system", "ticket_start"), ("pm", "stage_start")]
    assert ("pm", "start") not in kinds                        # 단계 시작 직후의 Agent Started 는 중복이라 생략
    tool = next(m for m in msgs if m["kind"] == "tool")
    assert tool["role"] == "pm" and tool["tool"] == "read_file" and "README.md" in tool["text"]
    ans = [m for m in msgs if m["kind"] == "answer"]
    assert [a["role"] for a in ans] == ["pm", "developer", "qa", "techlead"]
    assert ans[0]["text"] == "# PRD\n- 목표: 테스트 추가" and ans[0]["ticket"] == "HX-001" and ans[0]["stage"] == "pm"
    err = next(m for m in msgs if m["kind"] == "error")
    assert err["role"] == "developer" and "connection reset" in err["text"]
    verdict = next(m for m in msgs if m["kind"] == "verdict_pass")
    assert verdict["role"] == "qa" and verdict["ts"] == "2026-11-08 02:25:00"
    done = next(m for m in msgs if m["kind"] == "ticket_done")
    assert "review" in done["text"] and "25분" in done["text"]
    assert any(m["kind"] == "proposal" and m["role"] == "techlead" and "HX-002" in m["text"] for m in msgs)
    assert msgs[-1]["kind"] == "ticket_done" and "1건" in msgs[-1]["text"]
    # 상태: 끝났으니 아무도 working 이 아니고, 답을 낸 역할은 done
    st = p.status("shop", ["pm", "designer", "developer", "qa", "techlead"], running=True)
    assert st["pm"]["state"] == "done" and st["designer"]["state"] == "idle" and st["qa"]["state"] == "done" and st["techlead"]["state"] == "done"


def test_parser_is_incremental_and_tracks_working_role():
    p = CrewFeedParser()
    first = p.feed(LOG[:3])
    assert [m["kind"] for m in first] == ["info", "ticket_start", "stage_start"]
    st = p.status("shop", ["pm", "developer"], running=True)
    assert st["pm"]["state"] == "working" and st["pm"]["ticket"] == "HX-001" and st["developer"]["state"] == "idle"
    assert p.status("shop", ["pm"], running=False)["pm"]["state"] == "idle"   # 데몬이 죽었으면 작업 중일 수 없다
    more = p.feed(LOG[3:])
    assert len(first) + len(more) == len(p.messages) and more[0]["kind"] == "tool"


def test_roles_from_template_org():
    roles = roles_from_org(load_template())
    keys = [r["key"] for r in roles]
    assert keys == ["pm", "designer", "developer", "qa", "techlead"]
    tl = roles[-1]
    assert tl["stages"] == ["discover"] and tl["model"] == "qwen2.5-coder:7b" and tl["name_ko"] == "테크 리드"
    assert roles[2]["stages"] == ["dev"] and "write_file" in roles[2]["tools"]


def test_crew_api(tmp_path: Path, node_python_repo: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from test_monitor import collector, make_fleet

    from hermes_fleet.monitor.app import create_app

    write_ticket(node_python_repo / "hermes" / "backlog", "HX-001", "add tests", "P1", "backend", ["pm", "dev", "qa"], ["a"], "body", status="todo")
    commit_all(node_python_repo, "t")
    paths = make_fleet(tmp_path, node_python_repo, LOG)
    client = TestClient(create_app(collector(paths)))
    d = client.get("/api/crew").json()
    room = next(r for r in d["rooms"] if r["name"] == "shop")
    assert [r["key"] for r in room["roles"]] == ["pm", "designer", "developer", "qa", "techlead"]
    assert room["status"]["pm"]["state"] == "done" and room["active"] is False
    assert d["messages"][-1]["kind"] == "ticket_done" and d["total_messages"] == len(d["messages"])
    only = client.get("/api/crew?repo=shop&limit=5").json()
    assert len(only["messages"]) == 5 and all(m["repo"] in ("shop", None) for m in only["messages"])
    assert client.get("/api/crew?repo=nope").json()["messages"][0]["repo"] is None   # fleet 전체 메시지만 남는다
