"""신규 프로젝트: 로컬 풀스택 골격, 비공개 GitHub 원격, Hermes 첫 작업 티켓."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .backlog import write_ticket
from .fleetfile import set_repo_selected
from .repos import detect_profile

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")

def _dir(state_dir: Path) -> Path: return state_dir / "projects"
def _path(state_dir: Path, pid: str) -> Path: return _dir(state_dir) / f"{pid}.json"
def _save(path: Path, d: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"); os.replace(tmp, path)
def get(state_dir: Path, pid: str) -> dict[str, Any]:
    try: return json.loads(_path(state_dir, pid).read_text(encoding="utf-8"))
    except OSError as exc: raise ValueError("신규 프로젝트를 찾을 수 없습니다") from exc
def list_projects(state_dir: Path) -> list[dict[str, Any]]:
    out=[]
    for p in _dir(state_dir).glob("*.json"):
        try: out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError): pass
    return sorted(out, key=lambda x:x.get("created_at", ""), reverse=True)
def create(state_dir: Path, name: str, description: str) -> dict[str, Any]:
    name=name.strip().lower(); description=description.strip()
    if not _NAME.fullmatch(name): raise ValueError("프로젝트 이름은 영문 소문자·숫자·하이픈, 2~63자로 입력하세요")
    if len(description)<40 or len(description)>60000: raise ValueError("설명은 40~60,000자로 입력하세요")
    now=time.time(); pid=f"NP-{time.strftime('%Y%m%d-%H%M%S',time.localtime(now))}-{int(now%1*1000):03d}"
    d={"id":pid,"name":name,"description":description,"status":"queued","created_at":time.strftime("%Y-%m-%dT%H:%M:%S",time.localtime(now)),"updated_at":time.strftime("%Y-%m-%dT%H:%M:%S",time.localtime(now))}
    _save(_path(state_dir,pid),d); return d
def update(state_dir: Path,pid:str,**changes:Any)->dict[str,Any]:
    d=get(state_dir,pid); d.update(changes); d["updated_at"]=time.strftime("%Y-%m-%dT%H:%M:%S"); _save(_path(state_dir,pid),d); return d

def bootstrap(state_dir: Path, fleet_root: Path, fleet_config: Path, pid: str) -> dict[str, Any]:
    d=get(state_dir,pid); root=fleet_root/d["name"]
    if root.exists(): raise ValueError(f"로컬 경로가 이미 존재합니다: {root}")
    update(state_dir,pid,status="creating")
    try:
        (root/"frontend").mkdir(parents=True); (root/"backend").mkdir()
        (root/"README.md").write_text(f"# {d['name']}\n\n{d['description']}\n",encoding="utf-8")
        (root/"frontend"/"index.html").write_text("<!doctype html><html lang=\"ko\"><meta charset=\"utf-8\"><title>New project</title><body><main id=\"app\">프로젝트를 준비 중입니다.</main></body></html>\n",encoding="utf-8")
        (root/"backend"/"main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI(title='New Project API')\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n",encoding="utf-8")
        (root/"backend"/"requirements.txt").write_text("fastapi>=0.115,<1\nuvicorn[standard]>=0.30,<1\n",encoding="utf-8")
        subprocess.run(["git","init","-b","main"],cwd=root,check=True,capture_output=True,text=True)
        subprocess.run(["git","add","."],cwd=root,check=True); subprocess.run(["git","commit","-m","chore: initialize project scaffold"],cwd=root,check=True,capture_output=True,text=True)
        gh=subprocess.run(["gh","repo","create",d["name"],"--private","--source",str(root),"--remote","origin","--push"],check=True,capture_output=True,text=True)
        set_repo_selected(fleet_config,d["name"],True)
        profile=detect_profile(root)
        if not profile.eligible: raise ValueError("생성한 저장소의 FE/BE 프로필을 탐지하지 못했습니다")
        spec=root/"PROJECT.md"; spec.write_text(d["description"],encoding="utf-8")
        ticket=write_ticket(profile.backlog_dir,"NP-001","프로젝트 V1 기반 기능 구축","P1","fullstack",["pm","design","dev","qa"],["PROJECT.md의 요구사항을 단계적으로 구현한다","역할·권한·오류 처리를 검증한다"],"## 프로젝트 요구사항\n\n`PROJECT.md` 원문을 최우선 기준으로 사용한다.\n",status="todo")
        subprocess.run(["git","add","."],cwd=root,check=True); subprocess.run(["git","commit","-m","hermes: add initial V1 implementation ticket"],cwd=root,check=True,capture_output=True,text=True); subprocess.run(["git","push"],cwd=root,check=True,capture_output=True,text=True)
        return update(state_dir,pid,status="ready",path=str(root),github_url=gh.stdout.strip(),ticket="NP-001")
    except Exception as exc:
        update(state_dir,pid,status="failed",error=str(exc),path=str(root)); raise
