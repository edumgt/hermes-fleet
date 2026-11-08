"""일일 자동 머지: QA PASS(review) 브랜치를 기준 브랜치에 머지하고, 레포 프로필의 테스트·빌드가 통과하면 확정한다.

실패(충돌, 검증 실패)하면 머지를 되돌리고 티켓은 review 상태로 남겨 사람이 본다.
require_verification 이 켜져 있으면 실행 가능한 검증 단계가 없는 레포는 머지하지 않는다.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .backlog import Ticket, TicketError
from .repos import RepoProfile
from .settings import REPO_BACKLOG_DIR
from .tools import git
from .workflow import run_deploy

Logger = Callable[[str], None]
Verifier = Callable[[Path, Logger], tuple[bool, str]]
_BRANCH_TICKET = re.compile(r"^hermes/([A-Za-z]+-\d+)$")


@dataclass
class MergeConfig:
    push: bool = True
    delete_merged_branches: bool = True
    verify: bool = True
    require_verification: bool = True
    verify_timeout: int = 900

    @classmethod
    def from_dict(cls, data: dict, push_override: bool | None = None) -> "MergeConfig":
        return cls(push=bool(data.get("push", True)) if push_override is None else push_override,
                   delete_merged_branches=bool(data.get("delete_merged_branches", True)), verify=bool(data.get("verify", True)),
                   require_verification=bool(data.get("require_verification", True)), verify_timeout=int(data.get("verify_timeout", 900)))


@dataclass
class MergeOutcome:
    branch: str
    ticket_id: str
    result: str  # merged | conflict | verify-failed | skipped | error
    detail: str = ""


@dataclass
class MergeReport:
    repo: str
    started_at: str
    outcomes: list[MergeOutcome] = field(default_factory=list)
    pushed: bool = False
    note: str = ""
    deploy: dict | None = None     # fleet 모드 배포 결과 {ok, detail, at}

    @property
    def merged(self) -> list[MergeOutcome]:
        return [o for o in self.outcomes if o.result == "merged"]


def _run(args: list[str], root: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603


def list_ticket_branches(root: Path) -> list[tuple[str, str]]:
    out = _run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/hermes/"], root).stdout
    found = [(name, m.group(1)) for name in out.split() if (m := _BRANCH_TICKET.match(name))]
    return sorted(found)


def ticket_on_branch(root: Path, branch: str, ticket_id: str) -> Ticket | None:
    files = _run(["git", "ls-tree", "--name-only", branch, REPO_BACKLOG_DIR + "/"], root).stdout.split()
    for path in files:
        if Path(path).name.upper().startswith(ticket_id.upper() + "-"):
            text = _run(["git", "show", f"{branch}:{path}"], root).stdout
            try:
                return Ticket.from_markdown(text, root / path)
            except TicketError:
                return None
    return None


def profile_verify(profile: RepoProfile, root: Path, log: Logger, timeout: int = 900) -> tuple[bool, str]:
    """레포 프로필의 (cwd, command) 단계를 순서대로 실행. deps 명령은 node_modules 가 없을 때만."""
    ran = 0
    for subdir, cmd in profile.verify_steps():
        cwd = root / subdir
        if not cwd.is_dir():
            continue
        if cmd[:1] == ["npm"] and cmd[1:2] in (["ci"], ["install"]) and (cwd / "node_modules").exists():
            continue
        log(f"[merge][{profile.name}] verify: {' '.join(cmd)} ({subdir})")
        try:
            proc = _run(cmd, cwd, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"timeout: {' '.join(cmd)} in {subdir}"
        except FileNotFoundError as exc:
            return False, f"command not found: {exc}"
        if proc.returncode != 0:
            tail = (proc.stdout + "\n" + proc.stderr)[-3000:]
            return False, f"{' '.join(cmd)} failed in {subdir} (exit {proc.returncode}):\n{tail}"
        if not (cmd[:1] == ["npm"] and cmd[1:2] in (["ci"], ["install"])):
            ran += 1
    if ran == 0:
        return False, "no verification step could run"
    return True, f"{ran} verification step(s) passed"


def auto_merge(profile: RepoProfile, config: MergeConfig, log: Logger = print, verify: Verifier | None = None, dry_run: bool = False) -> MergeReport:
    root = profile.root
    base = profile.base_branch
    report = MergeReport(repo=profile.name, started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    verify = verify or (lambda r, lg: profile_verify(profile, r, lg, config.verify_timeout))

    if config.verify and config.require_verification and not profile.has_verification():
        report.note = "no runnable verification (tests/build) for this repo; auto-merge disabled, review branches left for a human"
        log(f"[merge][{profile.name}] {report.note}")
        return report
    if git.current_branch(root) != base:
        report.note = f"not on base branch '{base}'; skipped"
        log(f"[merge][{profile.name}] {report.note}")
        return report
    if not git.is_clean(root):
        report.note = f"{base} has uncommitted changes; skipped"
        log(f"[merge][{profile.name}] {report.note}")
        return report

    remote = profile.has_remote
    if remote:
        fetch = _run(["git", "fetch", "--prune", "origin"], root, timeout=120)
        if fetch.returncode != 0:
            report.note = f"git fetch failed: {fetch.stderr.strip()[-300:]}"
            log(f"[merge][{profile.name}] {report.note}; continuing offline")
            remote = False
        else:
            pull = _run(["git", "pull", "--rebase", "origin", base], root, timeout=120)
            if pull.returncode != 0:
                _run(["git", "rebase", "--abort"], root)
                report.note = f"git pull --rebase failed: {pull.stderr.strip()[-300:]}"
                log(f"[merge][{profile.name}] {report.note}; skipped")
                return report

    for branch, ticket_id in list_ticket_branches(root):
        ticket = ticket_on_branch(root, branch, ticket_id)
        if ticket is None:
            report.outcomes.append(MergeOutcome(branch, ticket_id, "skipped", "ticket file not found on branch"))
            continue
        if ticket.status != "review":
            report.outcomes.append(MergeOutcome(branch, ticket_id, "skipped", f"status={ticket.status} (needs review)"))
            continue
        if dry_run:
            report.outcomes.append(MergeOutcome(branch, ticket_id, "skipped", "dry-run: would merge"))
            log(f"[merge][{profile.name}] dry-run: would merge {branch} ({ticket.title})")
            continue
        log(f"[merge][{profile.name}] ▶ {branch}: {ticket.title}")
        head_before = _run(["git", "rev-parse", "HEAD"], root).stdout.strip()
        merge = _run(["git", "merge", "--no-ff", "--no-edit", "-m", f"merge({ticket_id}): {ticket.title}\n\nQA PASS 브랜치 자동 머지 (hermes-fleet merge)", branch], root)
        if merge.returncode != 0:
            _run(["git", "merge", "--abort"], root)
            _run(["git", "reset", "--hard", head_before], root)
            report.outcomes.append(MergeOutcome(branch, ticket_id, "conflict", (merge.stdout.strip() or merge.stderr.strip())[-500:]))
            log(f"[merge][{profile.name}] ✗ conflict on {branch}; left for a human")
            continue
        if config.verify:
            ok, detail = verify(root, log)
            if not ok:
                _run(["git", "reset", "--hard", head_before], root)
                report.outcomes.append(MergeOutcome(branch, ticket_id, "verify-failed", detail))
                log(f"[merge][{profile.name}] ✗ verification failed after merging {branch}; merge reverted")
                continue
        path = ticket.path if ticket.path and ticket.path.exists() else None
        if path:
            Ticket.from_markdown(path.read_text(encoding="utf-8"), path).set_status("done")
            _run(["git", "add", str(path)], root)
            _run(["git", "commit", "-q", "-m", f"chore({ticket_id}): status done"], root)
        if config.delete_merged_branches:
            _run(["git", "branch", "-d", branch], root)
        report.outcomes.append(MergeOutcome(branch, ticket_id, "merged", ticket.title))
        log(f"[merge][{profile.name}] ✓ merged {branch}")

    if report.merged and config.push and remote and not dry_run:
        push = _run(["git", "push", "origin", base], root, timeout=120)
        report.pushed = push.returncode == 0
        if report.pushed and config.delete_merged_branches:
            for o in report.merged:
                _run(["git", "push", "origin", "--delete", o.branch], root, timeout=60)
        log(f"[merge][{profile.name}] pushed to origin" if report.pushed else f"[merge][{profile.name}] push failed: {push.stderr.strip()[-300:]}")
        if report.pushed and profile.deploy.get("trigger") == "fleet" and profile.deploy.get("command"):
            log(f"[merge][{profile.name}] ▶ deploy ({profile.deploy.get('target') or 'fleet.yaml deploy.command'})")
            ok, detail = run_deploy(root, profile.deploy, log=log, timeout=config.verify_timeout)
            report.deploy = {"ok": ok, "detail": detail, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "target": profile.deploy.get("target")}
            log(f"[merge][{profile.name}] {'✓ deployed' if ok else '✗ deploy failed'}: {detail.splitlines()[0] if detail else ''}")
    _write_report(report, profile)
    return report


def _write_report(report: MergeReport, profile: RepoProfile) -> None:
    out = profile.workspace_dir / "merge-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"## {report.started_at}", ""]
    if report.note:
        lines.append(f"- note: {report.note}")
    for o in report.outcomes:
        lines.append(f"- {o.result:14s} {o.branch} ({o.ticket_id}) {o.detail.splitlines()[0] if o.detail else ''}")
    lines.append(f"- pushed: {report.pushed}")
    if report.deploy:
        lines.append(f"- deploy: {'ok' if report.deploy['ok'] else 'failed'} {report.deploy.get('target') or ''} — {str(report.deploy.get('detail', '')).splitlines()[0]}")
    with out.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")
