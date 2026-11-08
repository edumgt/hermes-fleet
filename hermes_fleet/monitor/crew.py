"""크루 룸 피드: daemon.log 를 역할(PM·디자이너·개발자·QA·Tech Lead)별 '대화' 로 바꾼다.

데몬의 구조화 로그(▶ ■ ✗, [repo][stage] iteration N → …, [qa] verdict)와 CrewAI verbose 패널(╭ 제목 ╮ … ╰ ╯:
Agent Started / Tool Execution / Agent Final Answer / Reasoning / 오류)을 한 줄씩 읽어 메시지 목록으로 만든다.
파서는 상태를 가지므로(마지막 시각, 레포별 진행 중 티켓·단계·역할, 열린 패널) SSE 로 새 줄을 이어서 넣을 수 있다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
TICKET_START = re.compile(r"^\[fleet\]\[([^\]]+)\] ▶ ([A-Za-z]+-\d+) (.*?) \[(P\d)\]$")
TICKET_DONE = re.compile(r"^\[fleet\]\[([^\]]+)\] ■ ([A-Za-z]+-\d+) → (\S+) on (\S+) \((\d+)s\)$")
TICKET_FAIL = re.compile(r"^\[fleet\]\[([^\]]+)\] ✗ ([A-Za-z]+-\d+) failed: (.*)$")
TICKET_ABORT = re.compile(r"^\[fleet\]\[([^\]]+)\] ⏹ ([A-Za-z]+-\d+) aborted: (.*)$")
STAGE_START = re.compile(r"^\[([^\]]+)\]\[(pm|design|dev|qa)\] iteration (\d+) → (.*)$")
STAGE_ABORT = re.compile(r"^\[([^\]]+)\]\[(pm|design|dev|qa)\] stop requested; aborting before stage$")
QA_VERDICT = re.compile(r"^\[qa\] iteration (\d+) verdict: (PASS|FAIL|UNKNOWN)$")
DISCOVER_START = re.compile(r"^\[fleet\]\[([^\]]+)\] ▶ discover \((.+)\)$")
DISCOVER_DONE = re.compile(r"^\[fleet\]\[([^\]]+)\] ■ (proposed (\d+) tickets|discover produced no new tickets)$")
DISCOVER_ITEM = re.compile(r"^\[discover\] (proposed (.+)|skip.*)$")
DISCOVER_NONE = re.compile(r"^\[discover\]\[([^\]]+)\] no parsable proposals in model output$")
MERGE_START = re.compile(r"^\[fleet\]\[([^\]]+)\] ▶ daily merge$")
MERGE_DONE = re.compile(r"^\[fleet\]\[([^\]]+)\] ■ daily merge: (.*)$")
MERGE_LINE = re.compile(r"^\[merge\]\[([^\]]+)\] (.*)$")
REQUEST_LINE = re.compile(r"^\[fleet\]\[([^\]]+)\] (▶|■) request (\w+)(.*)$")
FLEET_LINE = re.compile(r"^\[fleet\] (start|on-demand start|outside active hours|on-demand done|LLM endpoint unreachable|unexpected error)(.*)$")
PANEL_OPEN = re.compile(r"^\s*╭─*\s*(.*?)\s*─*╮?\s*$")
PANEL_CLOSE = re.compile(r"^\s*╰")
PANEL_LINE = re.compile(r"^\s*│(.*?)│?\s*$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

STAGE_KO = {"pm": "PRD", "design": "디자인 스펙", "dev": "구현", "qa": "검증", "discover": "발굴"}
DEFAULT_STAGE_ROLE = {"pm": "pm", "design": "designer", "dev": "developer", "qa": "qa", "discover": "techlead"}
DEFAULT_ROLE_TITLES = {"pm": "Product Manager", "designer": "UI/UX Designer", "developer": "Senior Full-Stack Developer",
                       "qa": "QA Automation Engineer", "techlead": "Tech Lead"}
ROLE_KO = {"pm": "PM", "designer": "디자이너", "developer": "개발자", "qa": "QA", "techlead": "테크 리드", "system": "시스템"}
ANSWER_LIMIT = 1800
TOOL_ARGS_LIMIT = 220


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n… ({len(text)}자)"


@dataclass
class RepoContext:
    ticket: str | None = None
    title: str | None = None
    stage: str | None = None          # pm|design|dev|qa|discover|merge
    iteration: int = 0
    role: str | None = None           # 현재 발언/작업 중인 역할 키
    since: str | None = None
    active: bool = False
    last_role_kind: dict[str, str] = field(default_factory=dict)   # 역할 → 마지막 메시지 종류(answer/error/…)
    last_role_ts: dict[str, str] = field(default_factory=dict)


class CrewFeedParser:
    """stateful. feed(lines) 가 새 메시지 목록을 돌려준다. role_for(repo, stage) / role_by_title(repo, title) 로 조직 정의를 반영."""

    def __init__(self, role_for: Callable[[str | None, str], str] | None = None,
                 role_by_title: Callable[[str | None, str], str | None] | None = None) -> None:
        self.role_for = role_for or (lambda repo, stage: DEFAULT_STAGE_ROLE.get(stage, "system"))
        self.role_by_title = role_by_title or self._default_role_by_title
        self.ts: str | None = None
        self.seq = 0
        self.repos: dict[str, RepoContext] = {}
        self.current_repo: str | None = None     # 패널(타임스탬프 없는 줄)이 속하는 레포
        self._panel_title: str | None = None
        self._panel: list[str] | None = None
        self.messages: list[dict[str, Any]] = []

    @staticmethod
    def _default_role_by_title(repo: str | None, title: str) -> str | None:
        t = title.strip().lower()
        for key, name in DEFAULT_ROLE_TITLES.items():
            if name.lower() == t:
                return key
        for key, needle in (("pm", "product"), ("designer", "design"), ("developer", "develop"), ("qa", "qa"), ("techlead", "tech lead")):
            if needle in t:
                return key
        return None

    # ---------- 공개 ----------
    def feed(self, lines: list[str]) -> list[dict[str, Any]]:
        start = len(self.messages)
        for raw in lines:
            self._line(ANSI.sub("", raw.rstrip("\n")))
        return self.messages[start:]

    def ctx(self, repo: str) -> RepoContext:
        return self.repos.setdefault(repo, RepoContext())

    def status(self, repo: str, roles: list[str], running: bool) -> dict[str, dict[str, Any]]:
        """역할별 상태: working | speaking | done | fail | idle (+since, ticket, stage)."""
        c = self.repos.get(repo) or RepoContext()
        out: dict[str, dict[str, Any]] = {}
        for r in roles:
            st = "idle"
            if running and c.active and c.role == r:
                st = "working"
            else:
                last = c.last_role_kind.get(r)
                if last in ("answer", "proposal", "verdict_pass"):
                    st = "done"
                elif last in ("error", "verdict_fail"):
                    st = "fail"
            out[r] = {"state": st, "since": c.last_role_ts.get(r) if st != "working" else c.since,
                      "ticket": c.ticket if (c.active and running) else None, "stage": c.stage if (c.active and running) else None}
        return out

    # ---------- 내부 ----------
    def _emit(self, repo: str | None, role: str, kind: str, text: str, **extra: Any) -> None:
        self.seq += 1
        c = self.repos.get(repo) if repo else None
        msg = {"id": self.seq, "ts": self.ts, "repo": repo, "role": role, "kind": kind, "text": text,
               "ticket": c.ticket if c else None, "stage": c.stage if c else None, "iteration": c.iteration if c else 0}
        msg.update(extra)
        self.messages.append(msg)
        if c and role != "system":
            c.last_role_kind[role] = kind
            c.last_role_ts[role] = self.ts or ""

    def _line(self, line: str) -> None:
        # 1) CrewAI 패널(타임스탬프 없음)
        if self._panel is not None:
            if PANEL_CLOSE.match(line):
                self._close_panel()
            else:
                m = PANEL_LINE.match(line)
                self._panel.append(m.group(1).rstrip() if m else line)
            return
        m = PANEL_OPEN.match(line)
        if m and "╭" in line:
            self._panel_title = m.group(1).strip("─ ").strip()
            self._panel = []
            return
        # 2) 데몬 로그 줄
        tm = LOG_TS.match(line)
        if not tm:
            return  # 패널 밖의 잡음(litellm 경고 등)은 무시
        self.ts, msg = tm.group(1), tm.group(2)
        self._structured(msg)

    def _structured(self, msg: str) -> None:  # noqa: C901 — 마커가 많다
        if (m := TICKET_START.match(msg)):
            repo, tid, title, prio = m.groups()
            c = self.ctx(repo)
            c.ticket, c.title, c.stage, c.iteration, c.role, c.since, c.active = tid, title, None, 0, None, self.ts, True
            c.last_role_kind.clear()
            self.current_repo = repo
            self._emit(repo, "system", "ticket_start", f"티켓 {tid} 작업 시작 — {title} [{prio}]", priority=prio)
            return
        if (m := STAGE_START.match(msg)):
            repo, stage, it, out = m.groups()
            c = self.ctx(repo)
            c.stage, c.iteration, c.role, c.since, c.active = stage, int(it), self.role_for(repo, stage), self.ts, True
            self.current_repo = repo
            nth = f" ({it}회차)" if int(it) > 1 else ""
            self._emit(repo, c.role, "stage_start", f"{STAGE_KO.get(stage, stage)} 작업을 시작합니다{nth}. 산출물: {out}", output=out)
            return
        if (m := STAGE_ABORT.match(msg)):
            repo, stage = m.groups()
            self._emit(repo, "system", "info", f"중지 요청으로 {STAGE_KO.get(stage, stage)} 단계 전에 멈춥니다.")
            return
        if (m := QA_VERDICT.match(msg)):
            it, verdict = m.groups()
            repo = self.current_repo
            if repo:
                c = self.ctx(repo)
                role = self.role_for(repo, "qa")
                kind = "verdict_pass" if verdict == "PASS" else "verdict_fail"
                text = {"PASS": f"검증 결과 PASS — {it}회차 구현을 승인합니다.", "FAIL": f"검증 결과 FAIL — {it}회차 구현에 결함이 있어 개발자에게 돌려보냅니다.",
                        "UNKNOWN": f"{it}회차 보고서에 VERDICT 가 없어 FAIL 로 처리합니다."}[verdict]
                self._emit(repo, role, kind, text, verdict=verdict)
                c.role = None
            return
        if (m := TICKET_DONE.match(msg)):
            repo, tid, status, branch, secs = m.groups()
            c = self.ctx(repo)
            self._emit(repo, "system", "ticket_done", f"티켓 {tid} 처리 완료 → {status} (브랜치 {branch}, {int(secs) // 60}분)", status=status)
            c.active, c.role, c.stage = False, None, None
            return
        if (m := TICKET_FAIL.match(msg)) or (m := TICKET_ABORT.match(msg)):
            repo, tid, why = m.groups()
            c = self.ctx(repo)
            word = "실패" if "failed" in msg else "중단"
            self._emit(repo, "system", "error", f"티켓 {tid} {word}: {_clip(why, 300)}")
            c.active, c.role, c.stage = False, None, None
            return
        if (m := DISCOVER_START.match(msg)):
            repo, who = m.groups()
            c = self.ctx(repo)
            role = self.role_by_title(repo, who) or self.role_for(repo, "discover")
            c.ticket, c.title, c.stage, c.iteration, c.role, c.since, c.active = None, None, "discover", 1, role, self.ts, True
            self.current_repo = repo
            self._emit(repo, role, "stage_start", "저장소를 둘러보고 다음에 처리할 개선 티켓을 찾아보겠습니다.")
            return
        if (m := DISCOVER_ITEM.match(msg)):
            repo = self.current_repo
            if repo:
                role = self.role_for(repo, "discover")
                body = m.group(1)
                if body.startswith("proposed "):
                    self._emit(repo, role, "proposal", f"새 티켓을 제안합니다: {m.group(2)}")
                else:
                    self._emit(repo, role, "info", f"제안 제외 — {body[5:].strip()}")
            return
        if (m := DISCOVER_NONE.match(msg)):
            repo = m.group(1)
            self._emit(repo, self.role_for(repo, "discover"), "info", "이번 분석에서는 쓸 만한 제안을 뽑지 못했습니다.")
            return
        if (m := DISCOVER_DONE.match(msg)):
            repo = m.group(1)
            c = self.ctx(repo)
            n = m.group(3)
            self._emit(repo, "system", "ticket_done", f"발굴 종료 — 제안 {n}건 (사람의 승인 대기)" if n else "발굴 종료 — 새 제안 없음")
            c.active, c.role, c.stage = False, None, None
            return
        if (m := MERGE_START.match(msg)):
            repo = m.group(1)
            c = self.ctx(repo)
            c.stage, c.role, c.since, c.active, c.ticket = "merge", None, self.ts, True, None
            self.current_repo = repo
            self._emit(repo, "system", "merge", "일일 자동 머지를 시작합니다 (QA PASS 브랜치 → 기준 브랜치, 검증 후 push).")
            return
        if (m := MERGE_DONE.match(msg)):
            repo = m.group(1)
            c = self.ctx(repo)
            self._emit(repo, "system", "merge", f"자동 머지 종료: {m.group(2)}")
            c.active, c.stage = False, None
            return
        if (m := MERGE_LINE.match(msg)):
            repo, body = m.groups()
            kind = "error" if "✗" in body or "failed" in body else "merge"
            self._emit(repo, "system", kind, body)
            return
        if (m := REQUEST_LINE.match(msg)):
            repo, mark, action, rest = m.groups()
            if mark == "▶":
                self.current_repo = repo
                self._emit(repo, "system", "info", f"요청 수신: {action}{rest.split(' (')[0]}")
            else:
                self._emit(repo, "system", "info", f"요청 {action} 결과{rest}")
            return
        if (m := FLEET_LINE.match(msg)):
            head, rest = m.groups()
            if head in ("start", "on-demand start"):
                self._emit(None, "system", "info", f"데몬 시작{rest}")
            elif head in ("outside active hours", "on-demand done"):
                self._emit(None, "system", "info", "데몬 종료")
                for c in self.repos.values():
                    c.active, c.role = False, None
            else:
                self._emit(None, "system", "error", f"{head}{rest}")
            return

    def _close_panel(self) -> None:
        title, body = self._panel_title or "", [ln.strip() for ln in (self._panel or [])]
        self._panel_title, self._panel = None, None
        repo = self.current_repo
        if repo is None:
            return
        c = self.ctx(repo)
        fields = self._fields(body)
        agent = fields.get("Agent")
        role = (self.role_by_title(repo, agent) if agent else None) or c.role or (self.role_for(repo, c.stage) if c.stage else "system")
        if "Agent Started" in title:
            c.role, c.active = role, True
            if c.last_role_kind.get(role) != "stage_start":   # 단계 시작 발언 직후면 같은 말을 반복하지 않는다
                self._emit(repo, role, "start", f"{agent or ROLE_KO.get(role, role)} 로서 과제를 받았습니다. 자료를 읽고 작업을 시작합니다.")
        elif "Final Answer" in title:
            text = self._after(body, "Final Answer:")
            self._emit(repo, role, "answer", _clip(text, ANSWER_LIMIT), chars=len(text))
        elif title.startswith("🧠") or "Reasoning" in title:
            text = self._after(body, "Reasoning:") or "\n".join(body)
            if text.strip():
                self._emit(repo, role, "thought", _clip(text, 600))
        elif "Tool Execution Started" in title or "LLM Tool Usage" in title or "Tool Usage" in title:
            tool = fields.get("Tool") or fields.get("Tool Name") or "?"
            args = fields.get("Tool Args") or ""
            self._emit(repo, role, "tool", f"🔧 {tool} {_clip(args, TOOL_ARGS_LIMIT)}".rstrip(), tool=tool)
        elif "Tool Failure" in title or "Tool Error" in title or "LLM Error" in title or "LLM Tool Error" in title or "Task Failure" in title or "Crew Execution Failed" in title:
            err = fields.get("Error") or self._after(body, "Error:") or "\n".join(body)
            self._emit(repo, role, "error", _clip(f"⚠ {title}: {err}", 500))
        # Task Started / Task Completion / Crew Execution Started|Completed / Tool Completed → 잡음, 생략

    @staticmethod
    def _fields(body: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for ln in body:
            if ":" in ln:
                k, v = ln.split(":", 1)
                k = k.strip()
                if k and " " not in k.strip("Tool Args") and k not in out and len(k) <= 24 and k[0].isupper():
                    out[k] = v.strip()
                elif k in ("Tool Args", "Tool Name", "Final Answer") and k not in out:
                    out[k] = v.strip()
        return out

    @staticmethod
    def _after(body: list[str], marker: str) -> str:
        for i, ln in enumerate(body):
            if ln.startswith(marker):
                first = ln[len(marker):].strip()
                rest = body[i + 1:]
                while rest and not rest[-1]:
                    rest.pop()
                return "\n".join(([first] if first else []) + rest)
        return ""


def roles_from_org(org: dict[str, Any]) -> list[dict[str, Any]]:
    """org.yaml → 크루 룸에 세울 역할 목록(단계 순서: pm, design, dev, qa, 그리고 발굴 역할)."""
    stage_role = {s: spec["role"] for s, spec in org.get("stages", {}).items()}
    order = [stage_role[s] for s in ("pm", "design", "dev", "qa") if s in stage_role]
    disc = (org.get("discover") or {}).get("role")
    if disc and disc not in order:
        order.append(disc)
    for k in org.get("roles", {}):
        if k not in order:
            order.append(k)
    out = []
    for key in order:
        spec = org["roles"].get(key, {})
        stages = [s for s, r in stage_role.items() if r == key] + (["discover"] if key == disc else [])
        out.append({"key": key, "title": spec.get("role", DEFAULT_ROLE_TITLES.get(key, key)), "name_ko": ROLE_KO.get(key, key),
                    "goal": spec.get("goal", ""), "model": spec.get("model"), "tools": list(spec.get("tools", [])), "stages": stages})
    return out
