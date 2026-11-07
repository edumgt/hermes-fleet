"""조직 정의 로딩: 레포의 hermes/config/org.yaml 이 있으면 우선, 없으면 fleet 템플릿 + 레포 프로필로 project 섹션을 채운다."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .repos import RepoProfile
from .settings import TEMPLATE_ORG


def _validate(org: dict[str, Any], source: Path) -> dict[str, Any]:
    for key in ("roles", "stages", "review_loop", "project"):
        if key not in org:
            raise ValueError(f"{source}: org.yaml missing '{key}'")
    for stage, spec in org["stages"].items():
        if spec.get("role") not in org["roles"]:
            raise ValueError(f"{source}: stage '{stage}' references unknown role '{spec.get('role')}'")
    return org


def load_template(path: Path = TEMPLATE_ORG) -> dict[str, Any]:
    return _validate(yaml.safe_load(path.read_text(encoding="utf-8")), path)


def load_org_for(profile: RepoProfile, template_path: Path = TEMPLATE_ORG) -> dict[str, Any]:
    override = profile.org_override
    if override.exists():
        org = _validate(yaml.safe_load(override.read_text(encoding="utf-8")), override)
        org.setdefault("project", {}).setdefault("name", profile.name)
        org["_source"] = str(override)
        return org
    org = load_template(template_path)
    fe = profile.frontend.label() if profile.frontend else "없음"
    be = profile.backend.label() if profile.backend else "없음"
    org["project"] = {
        "name": profile.name,
        "description": f"{profile.name} 저장소. 프런트엔드 {fe}, 백엔드 {be}. 두 영역이 하나의 서비스를 이루며 각자 독립적으로 테스트·빌드된다.",
        "conventions": list(org["project"].get("conventions", [])),
    }
    org["_source"] = str(template_path)
    return org
