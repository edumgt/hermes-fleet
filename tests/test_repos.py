from pathlib import Path

from conftest import commit_all, git_init

from hermes_fleet.repos import FleetConfig, detect_profile, scan_repos


def test_node_python_repo_is_eligible(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    assert p.eligible
    assert p.frontend.dir == "frontend" and p.frontend.kind == "node"
    assert p.frontend.test_cmd == ["npm", "test"] and p.frontend.build_cmd == ["npm", "run", "build"]
    assert p.frontend.deps_cmd[:2] == ["npm", "ci"]
    assert p.backend.dir == "." and p.backend.kind == "python"
    assert "--with-requirements" in p.backend.test_cmd and p.backend.test_cmd[-1] == "backend/tests"
    assert p.has_verification() and p.base_branch == "main"
    steps = p.verify_steps()
    assert steps[0][1][:2] == ["uv", "run"] and ("frontend", ["npm", "test"]) in steps


def test_spring_plus_static_frontend(tmp_path: Path):
    root = tmp_path / "lms"
    git_init(root)
    (root / "pom.xml").write_text("<project/>")
    (root / "src" / "main" / "java").mkdir(parents=True)
    (root / "fe" / "public").mkdir(parents=True)
    (root / "fe" / "public" / "index.html").write_text("<html></html>")
    commit_all(root)
    p = detect_profile(root)
    assert p.eligible
    assert p.frontend.dir == "fe" and p.frontend.kind == "static"
    assert p.backend.dir == "." and p.backend.kind == "java"
    assert p.backend.test_cmd and p.backend.test_cmd[0] in ("mvn", "./mvnw")


def test_root_react_frontend_with_backend_dir(tmp_path: Path):
    root = tmp_path / "saas"
    git_init(root)
    (root / "package.json").write_text('{"scripts":{"build":"vite build"},"devDependencies":{"vite":"5","react":"18"}}')
    (root / "src").mkdir()
    (root / "src" / "App.jsx").write_text("export default 1")
    (root / "backend").mkdir()
    (root / "backend" / "requirements.txt").write_text("fastapi")
    (root / "backend" / "tests").mkdir()
    (root / "backend" / "tests" / "test_a.py").write_text("def test_a(): pass")
    commit_all(root)
    p = detect_profile(root)
    assert p.eligible and p.frontend.dir == "." and p.frontend.kind == "node"
    assert p.frontend.test_cmd is None and p.frontend.build_cmd == ["npm", "run", "build"]
    assert p.backend.kind == "python"


def test_backend_only_and_non_git_are_not_eligible(tmp_path: Path):
    only_be = tmp_path / "api"
    git_init(only_be)
    (only_be / "app").mkdir()
    (only_be / "app" / "main.py").write_text("x=1")
    (only_be / "requirements.txt").write_text("fastapi")
    commit_all(only_be)
    p = detect_profile(only_be)
    assert not p.eligible and any("프런트엔드" in r for r in p.reasons)
    plain = tmp_path / "notes"
    plain.mkdir()
    assert not detect_profile(plain).eligible


def test_override_and_scan(tmp_path: Path, node_python_repo: Path):
    root = tmp_path / "rtc"
    git_init(root)
    for d in ("frontend", "signaling"):
        (root / d).mkdir()
        (root / d / "package.json").write_text('{"scripts":{"test":"node --test"}}' if d == "signaling" else '{"scripts":{"test":"vitest"},"devDependencies":{"vue":"3"}}')
    commit_all(root)
    (tmp_path / "skipme").mkdir()
    git_init(tmp_path / "skipme")
    cfg = FleetConfig(root=tmp_path, exclude=["skipme"], repos={"rtc": {"frontend": "frontend", "backend": "signaling"}})
    profiles = {p.name: p for p in scan_repos(cfg)}
    assert profiles["rtc"].eligible and profiles["rtc"].backend.dir == "signaling" and profiles["rtc"].backend.kind == "node"
    assert profiles["shop"].eligible
    assert profiles["skipme"].reasons == ["exclude 목록"]
    cfg.include = ["shop"]
    assert [p.name for p in scan_repos(cfg) if p.eligible] == ["shop"]


def test_fleet_config_load(tmp_path: Path):
    f = tmp_path / "fleet.yaml"
    f.write_text("root: /tmp/x\nactive_hours: '01:00-08:00'\nmerge_at: '07:30'\nexclude: [a]\nauto_merge: {push: false}\n")
    cfg = FleetConfig.load(f)
    assert cfg.root == Path("/tmp/x") and cfg.exclude == ["a"] and cfg.auto_merge["push"] is False and cfg.auto_merge["verify"] is True



def test_root_defaults_to_parent_and_relative_is_yaml_based(tmp_path):
    from hermes_fleet.repos import FleetConfig
    fleet = tmp_path / "work" / "hermes-fleet"
    fleet.mkdir(parents=True)
    y = fleet / "fleet.yaml"
    y.write_text("root:\n", encoding="utf-8")
    assert FleetConfig.load(y).root == (tmp_path / "work").resolve()          # 비우면 상위 폴더
    y.write_text("root: ../other\n", encoding="utf-8")
    assert FleetConfig.load(y).root == (tmp_path / "work" / "other").resolve()  # 상대 경로는 fleet.yaml 기준
    y.write_text(f"root: {tmp_path}\n", encoding="utf-8")
    assert FleetConfig.load(y).root == tmp_path
