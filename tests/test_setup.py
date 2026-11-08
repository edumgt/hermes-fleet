"""hermes-fleet init: 받은 환경에서 형제 레포를 스캔해 fleet.yaml/.env 를 만든다."""
from pathlib import Path

import pytest
from conftest import commit_all, git_init

from hermes_fleet import cli
from hermes_fleet.repos import FleetConfig
from hermes_fleet.setup import init_environment, next_steps

SRC = Path(__file__).resolve().parent.parent


def make_fleet_clone(tmp_path: Path) -> Path:
    fleet = tmp_path / "hermes-fleet"
    fleet.mkdir(parents=True)
    (fleet / "fleet.example.yaml").write_text((SRC / "fleet.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (fleet / ".env.example").write_text((SRC / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    return fleet


def make_sibling(tmp_path: Path, name: str, fe: bool = True) -> Path:
    r = tmp_path / name
    git_init(r)
    if fe:
        (r / "frontend").mkdir(); (r / "frontend" / "index.html").write_text("<html/>")
    (r / "backend").mkdir(); (r / "backend" / "main.py").write_text("x=1")
    commit_all(r)
    return r


def test_init_scans_siblings_and_writes_local_config(tmp_path: Path):
    fleet = make_fleet_clone(tmp_path)
    make_sibling(tmp_path, "web-shop"); make_sibling(tmp_path, "api-only", fe=False)
    logs = []
    res = init_environment(fleet, provider="ollama", model="hermes3:8b", base_url="http://llm:11434", log=logs.append,
                           endpoint_check=lambda s: {"ok": True, "url": s.base_url + "/v1/models", "models": ["hermes3:8b"]})
    assert res.selected == ["web-shop"] and {c.name for c in res.candidates} == {"web-shop", "api-only"}
    assert res.fleet_written and res.env_written and res.llm["ok"]
    cfg = FleetConfig.load(fleet / "fleet.yaml")
    assert cfg.root == tmp_path.resolve() and cfg.include == ["web-shop"] and cfg.approval_for("web-shop") == "auto"
    text = (fleet / "fleet.yaml").read_text(encoding="utf-8")
    assert 'root: ""' in text and "api-only" not in text                      # root 비움(상위 폴더) · 선택 안 한 레포 이름은 적지 않는다
    env = (fleet / ".env").read_text(encoding="utf-8")
    assert "HERMES_BASE_URL=http://llm:11434" in env and "HERMES_MODEL=hermes3:8b" in env
    assert (fleet / "state").is_dir() and (fleet / "logs").is_dir()
    assert any("① 환경 읽기" in m for m in logs) and any(step.startswith("scripts/monitor.sh up") for step in next_steps(res))
    # 두 번째 실행은 기존 파일을 보존한다
    res2 = init_environment(fleet, log=lambda m: None, endpoint_check=lambda s: {"ok": False, "url": "u", "error": "down"})
    assert not res2.fleet_written and not res2.env_written and any("연결하지 못했" in n for n in res2.notes)


def test_init_explicit_root_include_and_errors(tmp_path: Path):
    fleet = make_fleet_clone(tmp_path / "tools")
    make_sibling(tmp_path, "web-shop"); make_sibling(tmp_path, "crm")
    res = init_environment(fleet, root=tmp_path, include=["crm"], log=lambda m: None, endpoint_check=lambda s: {"ok": True, "url": "u", "models": []})
    cfg = FleetConfig.load(fleet / "fleet.yaml")
    assert cfg.root == tmp_path.resolve() and cfg.include == ["crm"] and f"root: {tmp_path}" in (fleet / "fleet.yaml").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        init_environment(fleet, root=tmp_path, include=["nope"], force=True, log=lambda m: None, endpoint_check=lambda s: {"ok": True, "url": "u", "models": []})


def test_cli_refuses_without_config_and_init_works(tmp_path: Path, capsys, monkeypatch):
    fleet = make_fleet_clone(tmp_path)
    make_sibling(tmp_path, "web-shop")
    missing = fleet / "fleet.yaml"
    assert cli.main(["--config", str(missing), "repos"]) == 2
    assert "init" in capsys.readouterr().err
    monkeypatch.setattr("hermes_fleet.setup.check_endpoint", lambda s: {"ok": True, "url": "u", "models": []})
    assert cli.main(["--config", str(missing), "init", "--yes", "--fleet-dir", str(fleet), "--provider", "ollama"]) == 0
    assert missing.exists() and "web-shop" in missing.read_text(encoding="utf-8")
    assert cli.main(["--config", str(missing), "repos"]) == 0



def test_setup_module_runs_standalone_without_crewai(tmp_path: Path):
    """컨테이너 자동 init 경로: python -m hermes_fleet.setup 은 crewai 없이 임포트·실행된다."""
    import subprocess
    import sys

    fleet = make_fleet_clone(tmp_path); make_sibling(tmp_path, "web-shop")
    probe = subprocess.run([sys.executable, "-c", "import sys, hermes_fleet.setup; print('crewai' in sys.modules)"], capture_output=True, text=True, cwd=str(SRC))
    assert probe.returncode == 0 and probe.stdout.strip() == "False"
    r = subprocess.run([sys.executable, "-m", "hermes_fleet.setup", "--fleet-dir", str(fleet), "--base-url", "http://127.0.0.1:9"], capture_output=True, text=True, cwd=str(SRC))
    assert r.returncode == 0 and (fleet / "fleet.yaml").exists() and "web-shop" in (fleet / "fleet.yaml").read_text(encoding="utf-8")
    assert "다음 단계" in r.stdout and "연결" in r.stdout      # LLM 점검은 실패해도 init 은 성공(안내만)
