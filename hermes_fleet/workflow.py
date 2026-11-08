"""전달 체인(delivery) 탐지와 워크플로 단계 계산.

레포의 성격에 따라 Hermes 의 작업이 어디까지 자동 전개되는지 — 티켓 → 크루 → 머지·검증 → GitHub push → CI/CD(GitHub Actions) → 배포(AWS 등) —
를 레포 안의 흔적(원격 URL, .github/workflows, 배포 스크립트·IaC 파일)과 fleet.yaml 의 `repos.<name>.deploy` 선언으로 판단한다.

판단 결과는 '모드'로 요약된다:
- push:   auto(원격이 있어 일일 머지 후 push) | none
- cicd:   auto(기준 브랜치 push 에 반응하는 워크플로가 있음) | none
- deploy: actions(push 에 반응하는 배포 워크플로) | fleet(fleet 이 push 뒤 deploy.command 실행) | manual(스크립트/IaC 는 있지만 사람이 실행) | none
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKIP = {".git", "node_modules", ".venv", "venv", "dist", "target", "__pycache__", "build", ".next", "hermes"}
MAX_SCAN_BYTES = 200_000
DEPLOYISH_NAME = re.compile(r"deploy|\bcd\b|release|publish|rollout|ship", re.I)

# (kind, regex) — 워크플로/스크립트 본문에서 배포 대상을 추정
TARGET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("aws-s3", re.compile(r"aws\s+s3\s+(sync|cp)\b", re.I)),
    ("aws-cloudfront", re.compile(r"cloudfront\s+create-invalidation", re.I)),
    ("aws-ec2", re.compile(r"\bEC2\b|appleboy/ssh-action|ssh\s+-i\s+\S+\s+\S+@|ec2-user@|ubuntu@\$\{\{", re.I)),
    ("aws-ecr", re.compile(r"\becr\b|amazon-ecr-login", re.I)),
    ("aws-ecs", re.compile(r"aws\s+ecs\s+|amazon-ecs-deploy|ecs-task-definition", re.I)),
    ("aws-lambda", re.compile(r"aws\s+lambda\s+update|serverless\s+deploy|\bsls\s+deploy", re.I)),
    ("aws-sam", re.compile(r"\bsam\s+(deploy|build)\b", re.I)),
    ("aws-cdk", re.compile(r"\bcdk\s+deploy\b", re.I)),
    ("aws-elastic-beanstalk", re.compile(r"\beb\s+deploy\b|elasticbeanstalk", re.I)),
    ("aws-codedeploy", re.compile(r"codedeploy|appspec", re.I)),
    ("aws-cloudformation", re.compile(r"aws\s+cloudformation\s+deploy", re.I)),
    ("aws-amplify", re.compile(r"amplify\s+(publish|push)", re.I)),
    ("github-pages", re.compile(r"actions-gh-pages|deploy-pages@|gh-pages", re.I)),
    ("vercel", re.compile(r"vercel", re.I)),
    ("netlify", re.compile(r"netlify", re.I)),
    ("docker-registry", re.compile(r"docker\s+push\b|docker/build-push-action", re.I)),
    ("kubernetes", re.compile(r"\bkubectl\b|\bhelm\s+(upgrade|install)", re.I)),
)
AWS_HINT = re.compile(r"\baws\b|amazonaws|aws-actions/|secrets\.AWS_", re.I)

# 레포 안 파일 기반 마커 (경로 글롭, 종류, 설명)
FILE_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("serverless.yml", "aws-lambda", "Serverless Framework"),
    ("serverless.yaml", "aws-lambda", "Serverless Framework"),
    ("samconfig.toml", "aws-sam", "AWS SAM 설정"),
    ("cdk.json", "aws-cdk", "AWS CDK 앱"),
    ("appspec.yml", "aws-codedeploy", "CodeDeploy appspec"),
    ("buildspec.yml", "aws-codebuild", "CodeBuild buildspec"),
    ("amplify.yml", "aws-amplify", "Amplify 빌드 설정"),
    (".elasticbeanstalk/config.yml", "aws-elastic-beanstalk", "Elastic Beanstalk CLI"),
    ("vercel.json", "vercel", "Vercel"),
    ("netlify.toml", "netlify", "Netlify"),
    ("fly.toml", "fly.io", "Fly.io"),
    ("render.yaml", "render", "Render"),
    ("Dockerfile", "docker", "Dockerfile"),
    ("docker-compose.prod.yml", "docker-compose", "운영용 compose"),
    ("k8s", "kubernetes", "k8s 매니페스트 디렉터리"),
    ("kubernetes", "kubernetes", "k8s 매니페스트 디렉터리"),
    ("terraform", "terraform", "Terraform 디렉터리"),
    ("deploy", "deploy-scripts", "deploy/ 디렉터리"),
    ("scripts/deploy.sh", "deploy-scripts", "배포 스크립트"),
    ("deploy.sh", "deploy-scripts", "배포 스크립트"),
)
SCRIPT_SUFFIXES = {".sh", ".bash", ".ps1", ".py", ".mk", ".tf", ".yml", ".yaml", ".toml", ".json"}
DEPLOY_SCRIPT_NAME = re.compile(r"deploy|publish|release|push|sync|upload|ship|infra|provision|rollout|invalidat", re.I)
ALWAYS_SCAN = {"Makefile", "package.json", "buildspec.yml", "samconfig.toml", "template.yaml", "serverless.yml"}


@dataclass
class Workflow:
    file: str
    name: str
    triggers: list[str]
    push_branches: list[str]
    on_base_push: bool          # 기준 브랜치 push(또는 그 뒤 workflow_run)에 반응
    deployish: bool
    targets: list[str]
    jobs: list[str]


@dataclass
class Delivery:
    remote_url: str | None = None
    remote_host: str = "none"            # github | gitlab | bitbucket | other | none
    remote_web: str | None = None
    owner: str | None = None
    repo: str | None = None
    workflows: list[Workflow] = field(default_factory=list)
    markers: list[dict[str, str]] = field(default_factory=list)   # {kind, path, note}
    targets: list[str] = field(default_factory=list)
    override: dict[str, Any] = field(default_factory=dict)        # fleet.yaml repos.<name>.deploy
    push_mode: str = "none"
    cicd_mode: str = "none"
    deploy_mode: str = "none"
    deploy_target: str | None = None
    aws: bool = False
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ---------- 원격 ----------

def parse_remote(url: str | None) -> tuple[str, str | None, str | None, str | None]:
    """(host, web_url, owner, repo)"""
    if not url:
        return "none", None, None, None
    m = re.match(r"^(?:https?://|git@|ssh://git@)([^/:]+)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not m:
        return "other", None, None, None
    host, owner, repo = m.group(1).lower(), m.group(2), m.group(3)
    kind = "github" if "github.com" in host else "gitlab" if "gitlab" in host else "bitbucket" if "bitbucket" in host else "other"
    return kind, f"https://{host}/{owner}/{repo}", owner, repo


def remote_url(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True, timeout=10, check=False).stdout.strip()  # noqa: S603,S607
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out or None


def sync_status(root: Path, base: str) -> dict[str, Any]:
    """로컬 기준 브랜치와 origin/<base> 의 ahead/behind (fetch 는 하지 않음 — 마지막 fetch 기준)."""
    try:
        out = subprocess.run(["git", "rev-list", "--left-right", "--count", f"{base}...origin/{base}"], cwd=root, capture_output=True, text=True, timeout=10, check=False)  # noqa: S603,S607
        if out.returncode != 0:
            return {"tracked": False}
        ahead, behind = (int(x) for x in out.stdout.split())
        last = subprocess.run(["git", "log", "-1", "--format=%ci", f"origin/{base}"], cwd=root, capture_output=True, text=True, timeout=10, check=False).stdout.strip()  # noqa: S603,S607
        return {"tracked": True, "ahead": ahead, "behind": behind, "origin_head_date": last}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"tracked": False}


# ---------- 탐지 ----------

def _targets_in(text: str) -> list[str]:
    return [kind for kind, pat in TARGET_PATTERNS if pat.search(text)]


def parse_workflow(path: Path, base_branch: str) -> Workflow | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_SCAN_BYTES]
        data = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    on = data.get("on", data.get(True))
    triggers: list[str] = []
    push_branches: list[str] = []
    on_base = False
    if isinstance(on, str):
        triggers = [on]
        on_base = on in ("push",)
    elif isinstance(on, list):
        triggers = [str(x) for x in on]
        on_base = "push" in triggers
    elif isinstance(on, dict):
        triggers = [str(k) for k in on]
        for key in ("push", "workflow_run"):
            spec = on.get(key)
            if key in on:
                branches = (spec or {}).get("branches") if isinstance(spec, dict) else None
                if branches is None:
                    on_base = True
                else:
                    bl = [str(b) for b in (branches if isinstance(branches, list) else [branches])]
                    if key == "push":
                        push_branches = bl
                    if any(b == base_branch or b in ("*", "**") or re.fullmatch(b.replace("*", ".*"), base_branch) for b in bl):
                        on_base = True
    jobs = list((data.get("jobs") or {}).keys()) if isinstance(data.get("jobs"), dict) else []
    name = str(data.get("name") or path.stem)
    targets = _targets_in(text)
    deployish = bool(DEPLOYISH_NAME.search(name) or DEPLOYISH_NAME.search(" ".join(jobs)) or DEPLOYISH_NAME.search(path.stem) or targets)
    return Workflow(file=path.name, name=name, triggers=triggers, push_branches=push_branches, on_base_push=on_base, deployish=deployish, targets=targets, jobs=jobs)


def scan_markers(root: Path) -> tuple[list[dict[str, str]], list[str]]:
    markers: list[dict[str, str]] = []
    targets: list[str] = []
    for rel, kind, note in FILE_MARKERS:
        p = root / rel
        if p.exists():
            markers.append({"kind": kind, "path": rel, "note": note})
    # 스크립트/Makefile/package.json 안의 배포 명령
    candidates: list[Path] = []
    for pat in ("*.sh", "Makefile", "package.json", "deploy/*", "scripts/*", "*.tf", "terraform/*.tf", "buildspec.yml", "samconfig.toml", "template.yaml"):
        candidates += [p for p in root.glob(pat) if p.is_file() and not any(part in SKIP for part in p.relative_to(root).parts)]
    seen: set[str] = set()
    for p in candidates[:200]:
        if p.suffix and p.suffix not in SCRIPT_SUFFIXES and p.name not in ("Makefile",):
            continue
        rel = p.relative_to(root).as_posix()
        # 일반 스크립트는 이름이 배포성일 때만 센다 (데모/테스트/유틸 스크립트에 적힌 aws 명령 문자열로 오탐하지 않도록). deploy/·*.tf·buildspec 등은 항상.
        if not (rel.startswith("deploy/") or p.suffix == ".tf" or p.name in ALWAYS_SCAN or DEPLOY_SCRIPT_NAME.search(p.name)):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:MAX_SCAN_BYTES]
        except OSError:
            continue
        hits = _targets_in(text)
        if p.suffix == ".tf" and AWS_HINT.search(text):
            hits.append("terraform-aws")
        if p.name == "buildspec.yml":
            hits.append("aws-codebuild")
        for h in hits:
            key = f"{h}:{p.relative_to(root).as_posix()}"
            if key in seen:
                continue
            seen.add(key)
            markers.append({"kind": h, "path": p.relative_to(root).as_posix(), "note": "명령/설정에서 탐지"})
            targets.append(h)
    return markers, targets


def detect_delivery(root: Path, base_branch: str = "main", override: dict[str, Any] | None = None) -> Delivery:
    d = Delivery(override=dict(override or {}))
    d.remote_url = remote_url(root)
    d.remote_host, d.remote_web, d.owner, d.repo = parse_remote(d.remote_url)
    wf_dir = root / ".github" / "workflows"
    if wf_dir.is_dir():
        for f in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
            wf = parse_workflow(f, base_branch)
            if wf:
                d.workflows.append(wf)
    d.markers, file_targets = scan_markers(root)
    targets: list[str] = []
    for wf in d.workflows:
        targets += wf.targets
    targets += file_targets + [m["kind"] for m in d.markers if m["kind"].startswith("aws-")]
    d.targets = sorted(set(targets))

    d.push_mode = "auto" if d.remote_url else "none"
    d.cicd_mode = "auto" if (d.remote_host == "github" and any(w.on_base_push for w in d.workflows)) else "none"
    auto_deploy = [w for w in d.workflows if w.on_base_push and w.deployish]
    ov = d.override
    if ov.get("trigger") == "fleet" and ov.get("command"):
        d.deploy_mode = "fleet"
    elif d.remote_host == "github" and auto_deploy:
        d.deploy_mode = "actions"
    elif ov.get("trigger") == "manual" or ov.get("command") or any(w.deployish for w in d.workflows) or [m for m in d.markers if m["kind"] not in ("docker",)]:
        d.deploy_mode = "manual"      # 배포 수단은 있으나 기준 브랜치 push 로 자동 실행되지는 않음(다른 브랜치·workflow_dispatch·스크립트)
    else:
        d.deploy_mode = "none"
    if ov.get("trigger") == "none":
        d.deploy_mode = "none"
    deploy_targets = sorted({t for w in auto_deploy for t in w.targets}) or [t for t in d.targets if t.startswith(("aws-", "terraform", "github-pages", "vercel", "netlify", "kubernetes", "fly", "render"))]
    d.deploy_target = str(ov.get("target")) if ov.get("target") else (", ".join(deploy_targets) if deploy_targets else None)
    d.aws = bool(ov.get("target", "").startswith("aws") if isinstance(ov.get("target"), str) else False) or any(t.startswith(("aws-", "terraform-aws")) for t in (deploy_targets or d.targets))
    d.summary = summarize(d)
    return d


def summarize(d: Delivery) -> str:
    chain = ["티켓 → 크루 → 머지"]
    if d.push_mode == "auto":
        chain.append(f"push({d.remote_host})")
    if d.cicd_mode == "auto":
        chain.append("Actions")
    if d.deploy_mode == "actions":
        chain.append(f"배포 {d.deploy_target or ''}".strip() + " (push 시 자동)")
    elif d.deploy_mode == "fleet":
        chain.append(f"배포 {d.deploy_target or ''}".strip() + " (fleet 이 실행)")
    elif d.deploy_mode == "manual":
        chain.append(f"배포 {d.deploy_target or ''}".strip() + " (수동)")
    return " → ".join(chain)


# ---------- 배포 실행 (fleet 모드 / deploy 요청) ----------

def run_deploy(root: Path, deploy: dict[str, Any], log=print, timeout: int = 900) -> tuple[bool, str]:
    cmd = deploy.get("command")
    if not cmd:
        return False, "deploy.command 가 fleet.yaml 에 없습니다"
    cwd = root / str(deploy.get("cwd") or ".")
    log(f"[deploy] $ {cmd}  (cwd: {cwd})")
    try:
        proc = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=int(deploy.get("timeout") or timeout), check=False)  # noqa: S602
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s: {cmd}"
    except OSError as exc:
        return False, f"cannot run: {exc}"
    tail = (proc.stdout + "\n" + proc.stderr).strip()[-3000:]
    return proc.returncode == 0, f"exit {proc.returncode}\n{tail}"


# ---------- GitHub Actions 실행 상태 ----------

_runs_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def github_runs(owner: str, repo: str, branch: str, token: str | None = None, ttl: float = 120.0, timeout: float = 6.0) -> dict[str, Any]:
    """기준 브랜치의 최근 워크플로 실행(워크플로별 최신 1건). 비공개 레포는 토큰 필요. 실패해도 예외 없이 {error} 를 돌려준다."""
    key = f"{owner}/{repo}@{branch}"
    hit = _runs_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs?branch={branch}&per_page=20"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hermes-fleet-monitor"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        latest: dict[str, dict[str, Any]] = {}
        for run in data.get("workflow_runs", []):
            name = run.get("name") or str(run.get("workflow_id"))
            if name not in latest:
                latest[name] = {"name": name, "status": run.get("status"), "conclusion": run.get("conclusion"), "event": run.get("event"),
                                "url": run.get("html_url"), "updated_at": run.get("updated_at"), "sha": (run.get("head_sha") or "")[:7], "title": run.get("display_title")}
        out = {"ok": True, "runs": list(latest.values()), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "web": f"https://github.com/{owner}/{repo}/actions"}
    except urllib.error.HTTPError as exc:
        out = {"ok": False, "error": f"HTTP {exc.code}" + (" (비공개 레포: MONITOR_GITHUB_TOKEN 필요)" if exc.code in (401, 403, 404) else ""), "runs": [], "web": f"https://github.com/{owner}/{repo}/actions"}
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": str(exc)[:200], "runs": [], "web": f"https://github.com/{owner}/{repo}/actions"}
    _runs_cache[key] = (time.time(), out)
    return out
