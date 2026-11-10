"""레포 판정(review): Docker 모니터 재기동 때(또는 요청 시) root 아래 저장소마다 '프로젝트로 진행할지' 를 Jev 로 판정해 fleet.yaml include/exclude 를 고친다.

LLM 에게 줄글을 시키지 않는다. 각 레포의 사실(FE/BE 탐지, 원격, 최근 커밋 수·날짜, 지난 판정 이후 바뀐 커밋/디렉터리, 티켓 수)을 JSON state 로 넘기고
Jev 의 타입 있는 답만 받는다:
  proceed  (choice) proceed|skip — 자율 크루가 이 레포를 프로젝트로 다룰지. probabilities + confidence
  activity (score)  0~3 — 커밋 이력으로 본 개발 활성도
  changed  (noul)   지난 판정 이후 판단에 영향을 주는 변화가 있었는지
정책(코드가 결정, 모델은 판정만):
  - hermes-fleet 자신과 review.pinned 레포는 바꾸지 않는다
  - FE·BE 가 모두 탐지되지 않은 레포는 Jev 가 proceed 라 해도 대상이 될 수 없다(기록만; repos.<name> 오버라이드 안내)
  - confidence < review.min_confidence 면 현 상태 유지
  - 진행 중(in-progress/review) 티켓이 있는 레포는 제외하지 않는다(머지 대기 작업 보호)
  - review.mode: apply = fleet.yaml 수정 / advise = state/review.json 에 판정만 기록
결과는 state/review.json(마지막 판정 + 레포별 HEAD) 과 logs/review.log 에 남고 모니터 레포 목록의 'Jev' 열과 /api/review 로 보인다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import jev
from .backlog import VALID_STATUS, TicketError, load_tickets
from .fleetfile import FleetFileError, is_selected, set_repo_selected
from .repos import FleetConfig, RepoProfile, detect_profile
from .tools import git

REVIEW_STATE = "review.json"
REVIEW_LOG = "review.log"
IN_PROGRESS = ("in-progress", "review")
ACTIVITY_LEVELS = ["Archived or abandoned: no commits for many months", "Dormant: rare commits, months apart",
                   "Occasional: a few commits a month", "Active: regular commits in recent weeks"]

Decider = Callable[[Any, dict[str, dict[str, Any]]], dict[str, Any]]


@dataclass
class ReviewSettings:
    on_start: bool = True
    mode: str = "apply"             # apply | advise
    model: str | None = None        # None → JEV_MODEL/.env → jev-latest
    min_confidence: float = 0.6
    pinned: list[str] = field(default_factory=list)
    max_commits: int = 15
    active_days: int = 30

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "ReviewSettings":
        raw = raw or {}
        mode = str(raw.get("mode") or "apply").lower()
        if mode not in ("apply", "advise"):
            raise ValueError(f"review.mode 는 apply|advise 이어야 합니다: {mode!r}")
        return cls(on_start=bool(raw.get("on_start", True)), mode=mode, model=(str(raw["model"]).strip() or None) if raw.get("model") else None,
                   min_confidence=float(raw.get("min_confidence", 0.6)), pinned=[str(x) for x in (raw.get("pinned") or [])],
                   max_commits=int(raw.get("max_commits", 15)), active_days=int(raw.get("active_days", 30)))


# ---------- 사실 수집 (Jev state) ----------

def _git(args: list[str], root: Path, timeout: int = 30) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, check=False).stdout.strip()  # noqa: S603,S607
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _commit_exists(root: Path, sha: str | None) -> bool:
    return bool(sha) and subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=root, capture_output=True, check=False).returncode == 0  # noqa: S603,S607


def _ticket_counts(profile: RepoProfile) -> tuple[dict[str, int], str | None]:
    counts = {st: 0 for st in VALID_STATUS}
    try:
        for t in load_tickets(profile.backlog_dir):
            counts[t.status] = counts.get(t.status, 0) + 1
    except TicketError as exc:
        return counts, str(exc)
    return counts, None


def repo_facts(profile: RepoProfile, selected: bool, previous_head: str | None, settings: ReviewSettings, now: dt.datetime | None = None) -> tuple[dict[str, Any], str | None]:
    """(Jev 에 넘길 한 레포의 사실, HEAD). 숫자·날짜 비교는 모델이 아니라 여기서 계산한다(Jev 는 산술·날짜 비교에 약하다)."""
    now = now or dt.datetime.now()
    root = profile.root
    head = _git(["rev-parse", "HEAD"], root) or None
    last = _git(["log", "-1", "--format=%cI%x09%s"], root).split("\t")
    days_since = None
    last_subject = None
    if len(last) == 2 and last[0]:
        try:
            last_dt = dt.datetime.fromisoformat(last[0])
            days_since = max(0, (now.astimezone() - last_dt).days) if last_dt.tzinfo else max(0, (now - last_dt).days)
        except ValueError:
            pass
        last_subject = last[1][:120]
    since = (now - dt.timedelta(days=settings.active_days)).strftime("%Y-%m-%d")
    recent = _git(["rev-list", "--count", f"--since={since}", "HEAD"], root)
    total = _git(["rev-list", "--count", "HEAD"], root)
    changes: dict[str, Any]
    if previous_head and head and previous_head != head and _commit_exists(root, previous_head):
        rng = f"{previous_head}..HEAD"
        lines = [ln for ln in _git(["log", "--format=%h %s", rng], root).splitlines() if ln]
        files = [f for f in _git(["diff", "--name-only", rng], root).splitlines() if f]
        dirs = sorted({f.split("/", 1)[0] if "/" in f else "(root)" for f in files})
        changes = {"status": "changed", "previous_head": previous_head[:12], "commit_count": len(lines), "commits": [ln[:120] for ln in lines[: settings.max_commits]],
                   "changed_files": len(files), "changed_top_level_dirs": dirs[:30]}
    elif previous_head and head == previous_head:
        changes = {"status": "unchanged", "previous_head": previous_head[:12], "commit_count": 0}
    else:
        changes = {"status": "first_review" if not previous_head else "previous_head_missing", "commit_count": None,
                   "recent_commits": [ln[:120] for ln in _git(["log", f"-{settings.max_commits}", "--format=%h %s"], root).splitlines() if ln]}
    counts, backlog_error = _ticket_counts(profile) if profile.eligible else ({}, None)
    facts: dict[str, Any] = {
        "repository": profile.name,
        "currently_selected_as_project": selected,
        "frontend": {"dir": profile.frontend.dir, "stack": profile.frontend.kind, "has_test_or_build": bool(profile.frontend.test_cmd or profile.frontend.build_cmd)} if profile.frontend else None,
        "backend": {"dir": profile.backend.dir, "stack": profile.backend.kind, "has_test_or_build": bool(profile.backend.test_cmd or profile.backend.build_cmd)} if profile.backend else None,
        "detection_notes": list(profile.reasons),
        "git": {"has_remote": profile.has_remote, "base_branch": profile.base_branch, "head": (head or "")[:12], "total_commits": int(total) if total.isdigit() else None,
                "days_since_last_commit": days_since, f"commits_last_{settings.active_days}_days": int(recent) if recent.isdigit() else None, "last_commit_subject": last_subject,
                "working_tree_clean": git.is_clean(root)},
        "changes_since_previous_review": changes,
        "hermes_tickets": counts,
        "has_in_progress_work": any(counts.get(s, 0) for s in IN_PROGRESS),
    }
    if backlog_error:
        facts["hermes_tickets_error"] = backlog_error
    return facts, head


def questions(settings: ReviewSettings) -> dict[str, dict[str, Any]]:
    return {
        "proceed": {
            "type": "choice",
            "instructions": ("An autonomous software crew (PM → Designer → Dev ⇄ QA, running nightly, auto-merging QA-passed branches) "
                             "improves repositories that have both a frontend and a backend. Should it take this repository on as a project?"),
            "criteria": {
                "proceed": "A real, maintained full-stack project with both frontend and backend code that is worth improving autonomously",
                "skip": "Not a fit: frontend or backend missing, archived/dormant for a long time, a lab/sandbox/tutorial/experiment, generated or vendored code, or too risky to change automatically",
            },
        },
        "activity": {"type": "score", "instructions": "How actively is this repository being developed, judging only from the git facts (days since last commit, recent commit count)", "criteria": ACTIVITY_LEVELS},
        "changed": {"type": "noul", "instructions": "Since the previous review, the repository changed in a way that matters for deciding whether to work on it",
                    "criteria": {"true": "New or removed frontend/backend code, structural changes, or activity that clearly resumed or stopped",
                                 "false": "No change, cosmetic changes only, or this is the first review"}},
    }


# ---------- 정책 ----------

def decide(facts: dict[str, Any], answers: dict[str, Any], settings: ReviewSettings, fleet_name: str) -> dict[str, Any]:
    name = facts["repository"]
    selected = bool(facts["currently_selected_as_project"])
    eligible = bool(facts["frontend"] and facts["backend"])
    choice, confidence, probs = jev.choice_of(answers["proceed"])
    score, label, _ = jev.score_of(answers["activity"]) if "activity" in answers else (0.0, "", 0.0)
    changed = jev.noul_of(answers["changed"]) if "changed" in answers else 0.0
    out = {"name": name, "eligible": eligible, "selected_before": selected,
           "jev": {"choice": choice, "proceed_probability": round(probs.get("proceed", 0.0), 3), "confidence": round(confidence, 3),
                   "activity_score": round(score, 2), "activity": label, "changed_probability": round(changed, 3)}}
    if name == fleet_name:
        return {**out, "decision": "keep", "reason": "하네스 자신(hermes-fleet)은 Jev 가 바꾸지 않음"}
    if name in settings.pinned:
        return {**out, "decision": "keep", "reason": "review.pinned 고정"}
    if not eligible:
        hint = " — Jev 는 진행을 권하지만 FE/BE 탐지가 안 돼 대상이 될 수 없음(fleet.yaml repos.<name>.frontend/backend 지정)" if choice == "proceed" else ""
        return {**out, "decision": "keep", "reason": f"FE·BE 미탐지{hint}"}
    if confidence < settings.min_confidence:
        return {**out, "decision": "keep", "reason": f"confidence {confidence:.2f} < {settings.min_confidence:.2f} → 현 상태 유지"}
    if choice == "proceed" and not selected:
        return {**out, "decision": "include", "reason": f"Jev proceed {probs.get('proceed', 0):.2f} (conf {confidence:.2f}) · 활성도 {label or score}"}
    if choice == "skip" and selected:
        if facts.get("has_in_progress_work"):
            return {**out, "decision": "keep", "reason": f"Jev skip {probs.get('skip', 0):.2f} 이지만 진행 중/리뷰 대기 티켓이 있어 보호"}
        return {**out, "decision": "exclude", "reason": f"Jev skip {probs.get('skip', 0):.2f} (conf {confidence:.2f}) · 활성도 {label or score}"}
    return {**out, "decision": "keep", "reason": f"Jev {choice} {probs.get(choice, 0):.2f} — 현 상태와 같음"}


# ---------- 실행 ----------

def load_last(fleet_dir: Path) -> dict[str, Any]:
    path = fleet_dir / "state" / REVIEW_STATE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _log(fleet_dir: Path, msg: str) -> None:
    try:
        (fleet_dir / "logs").mkdir(exist_ok=True)
        with (fleet_dir / "logs" / REVIEW_LOG).open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _candidates(cfg: FleetConfig, fleet_name: str) -> list[tuple[RepoProfile, bool]]:
    """root 아래 모든 git 레포(exclude 포함 — 되살릴지도 판정) 와 현재 선택 여부."""
    out: list[tuple[RepoProfile, bool]] = []
    if not cfg.root.is_dir():
        return out
    include = [str(x) for x in (cfg.include or [])]
    exclude = [str(x) for x in (cfg.exclude or [])]
    for child in sorted(cfg.root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith(".") or not git.is_repo(child):
            continue
        profile = detect_profile(child, (cfg.repos or {}).get(child.name))
        out.append((profile, is_selected(child.name, include, exclude)))
    return out


def run_review(fleet_dir: Path, cfg: FleetConfig, trigger: str = "manual", decider: Decider | None = None, dry_run: bool = False,
               now: dt.datetime | None = None, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """레포마다 Jev 를 한 번씩 호출해 판정·적용하고 state/review.json 을 쓴다. 실패해도 예외 대신 ok=False 결과를 기록한다."""
    fleet_dir = fleet_dir.resolve()
    now = now or dt.datetime.now()
    started = time.time()
    settings = ReviewSettings.from_config(cfg.review)
    jset = jev.JevSettings.from_env(settings.model)
    previous = load_last(fleet_dir)
    heads: dict[str, str] = dict(previous.get("heads") or {})
    say = log or (lambda m: None)
    result: dict[str, Any] = {"at": now.strftime("%Y-%m-%dT%H:%M:%S"), "trigger": trigger, "mode": "dry-run" if dry_run else settings.mode,
                              "model_requested": jset.model, "model": None, "base_url": jset.base_url, "ok": False, "error": None,
                              "repos": [], "heads": heads, "applied": 0, "usage": {"input_tokens": 0, "output_tokens": 0}, "duration_seconds": 0}
    config_path = cfg.source or (fleet_dir / "fleet.yaml")
    call: Decider = decider or (lambda state, qs: jev.decide(state, qs, jset))
    if decider is None and not jset.configured:
        result["error"] = "TYPESAFE_API_KEY 없음 — hermes-fleet/.env 에 넣으세요 (https://console.typesafe.ai/keys)"
        _finish(fleet_dir, result, started, say)
        return result
    qs = questions(settings)
    fleet_name = fleet_dir.name
    say(f"[review] start trigger={trigger} mode={result['mode']} model={jset.model} root={cfg.root}")
    try:
        for profile, selected in _candidates(cfg, fleet_name):
            facts, head = repo_facts(profile, selected, heads.get(profile.name), settings, now)
            answers_payload = call(facts, qs)
            answers = answers_payload.get("answers") or {}
            usage = answers_payload.get("usage") or {}
            for k in ("input_tokens", "output_tokens"):
                result["usage"][k] += int(usage.get(k) or 0)
            result["model"] = result["model"] or answers_payload.get("model")
            row = decide(facts, answers, settings, fleet_name)
            row.update({"head": (head or "")[:12], "changes": facts["changes_since_previous_review"], "applied": False, "selected_after": selected,
                        "days_since_last_commit": facts["git"]["days_since_last_commit"], "recent_commits": facts["git"].get(f"commits_last_{settings.active_days}_days")})
            if row["decision"] in ("include", "exclude") and settings.mode == "apply" and not dry_run:
                try:
                    set_repo_selected(config_path, profile.name, row["decision"] == "include")
                    row["applied"] = True
                    row["selected_after"] = row["decision"] == "include"
                    result["applied"] += 1
                except (FleetFileError, OSError) as exc:
                    row["reason"] += f" · 적용 실패: {exc}"
            if head:
                heads[profile.name] = head
            result["repos"].append(row)
            say(f"[review][{profile.name}] {row['decision']}{' ✓' if row['applied'] else ''} — {row['reason']}")
        result["ok"] = True
    except jev.JevError as exc:
        result["error"] = str(exc)
        result["error_status"] = exc.status
    except Exception as exc:  # noqa: BLE001 — 재기동 흐름을 막지 않는다
        result["error"] = f"{type(exc).__name__}: {exc}"
    _finish(fleet_dir, result, started, say)
    return result


def _finish(fleet_dir: Path, result: dict[str, Any], started: float, say: Callable[[str], None]) -> None:
    result["duration_seconds"] = round(time.time() - started, 2)
    summary = f"[review] done ok={result['ok']} repos={len(result['repos'])} applied={result['applied']} {result['duration_seconds']}s" + (f" error={result['error']}" if result["error"] else "")
    say(summary)
    _log(fleet_dir, summary)
    for r in result["repos"]:
        _log(fleet_dir, f"[review][{r['name']}] {r['decision']}{' applied' if r['applied'] else ''} conf={r['jev']['confidence']} p={r['jev']['proceed_probability']} — {r['reason']}")
    try:
        (fleet_dir / "state").mkdir(exist_ok=True)
        path = fleet_dir / "state" / REVIEW_STATE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        say(f"[review] state 저장 실패: {exc}")


def on_start_enabled(cfg: FleetConfig) -> bool:
    """컨테이너/모니터 기동 때 실행할지: HERMES_REVIEW=skip 이면 안 함, 아니면 fleet.yaml review.on_start."""
    if os.environ.get("HERMES_REVIEW", "auto").strip().lower() in ("skip", "off", "0", "false", "no"):
        return False
    try:
        return ReviewSettings.from_config(cfg.review).on_start
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    """`python -m hermes_fleet.review --fleet-dir DIR [--dry-run] [--trigger start]` — crewai 없이 동작(모니터 컨테이너·엔트리포인트용)."""
    import argparse

    ap = argparse.ArgumentParser(prog="hermes_fleet.review", description="Jev 로 레포별 '프로젝트로 진행할지' 판정 → fleet.yaml include/exclude 갱신")
    ap.add_argument("--fleet-dir", required=True)
    ap.add_argument("--config", help="fleet.yaml 경로(기본 <fleet-dir>/fleet.yaml)")
    ap.add_argument("--trigger", default="cli")
    ap.add_argument("--dry-run", action="store_true", help="판정만 출력(fleet.yaml 수정 없음)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    fleet_dir = Path(a.fleet_dir)
    cfg_path = Path(a.config) if a.config else fleet_dir / "fleet.yaml"
    if not cfg_path.exists():
        print(f"✗ fleet.yaml 이 없습니다: {cfg_path}")
        return 2
    res = run_review(fleet_dir, FleetConfig.load(cfg_path), trigger=a.trigger, dry_run=a.dry_run, log=print if not a.json else None)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
