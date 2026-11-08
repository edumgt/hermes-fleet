"""프런트엔드 기술 가이드(org.yaml frontend_guide)가 프롬프트와 레포 org 오버라이드에 주입되는지."""
from pathlib import Path

import yaml

from hermes_fleet import prompts
from hermes_fleet.discover import discover_prompt
from hermes_fleet.org import load_org_for, load_template
from hermes_fleet.repos import detect_profile

KEYWORDS = ("AG Grid Community", "ApexCharts", "Lightweight Charts", "Tailwind", "Pretendard", "오프캔버스", "햄버거", "1920×1080", "모달 팝업")


def test_template_guide_renders_with_all_stack_items():
    org = load_template()
    g = org["frontend_guide"]
    assert [i["area"] for i in g["stack"]] == ["CSS", "폰트", "표", "레이아웃·내비게이션", "차트", "테마", "날짜·시간 표시", "화면 규격", "입출력"]
    text = prompts.render_frontend_guide(g)
    for k in KEYWORDS:
        assert k in text
    assert "ag-grid-community@34.2.0" in text and "apexcharts@4.7.0" in text and "lightweight-charts@5.2.1" in text and "pretendard@v1.3.9" in text
    assert "### 도입 방식" in text and "### 검수 기준" in text and "### 화면 품질 기준" in text and "빈 띠" in text and "EDUMGT" in text and "data-theme" in text
    assert "ag-grid-enterprise" in text and "latest" in text
    assert "'VERDICT: FAIL' 의 근거" in text and "{" not in text   # YAML 매핑으로 잘못 파싱되지 않았는지
    assert prompts.render_frontend_guide(None) == "" and prompts.render_frontend_guide(False) == ""


def test_prompts_include_guide_only_when_given(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    guide = prompts.render_frontend_guide(load_template()["frontend_guide"])
    pm = prompts.pm_task("T", {"name": "x", "description": "d", "conventions": []}, p.profile_text(), frontend_guide=guide)
    design = prompts.design_task("T", "PRD", "p", p.profile_text(), frontend_guide=guide)
    dev = prompts.dev_task("T", "PRD", "p", None, None, None, 1, p.profile_text(), frontend_guide=guide)
    qa = prompts.qa_task("T", "P", "p", "D", "d", 1, p.profile_text(), frontend_guide=guide)
    for text, note in ((pm, "PM 참고"), (design, "디자이너 참고"), (dev, "개발자 참고"), (qa, "QA 참고")):
        assert note in text
        for k in KEYWORDS:
            assert k in text
    assert "## 7. 의존성" in design and "오프캔버스 좌측 사이드바 안에 배치" in design and "FE-GUIDE" in qa and "5. 프런트엔드 변경이 있으면" in qa
    # 가이드를 안 넘기면(끈 레포) 흔적이 없다
    assert "Pretendard" not in prompts.dev_task("T", "PRD", "p", None, None, None, 1, p.profile_text())
    assert "참고:" not in prompts.qa_task("T", "P", "p", "D", "d", 1, p.profile_text())


def test_discover_prompt_includes_guide(node_python_repo: Path):
    p = detect_profile(node_python_repo)
    org = load_org_for(p)
    text = discover_prompt(org, p, [], 2, "digest")
    assert "Tech Lead 참고" in text and "AG Grid Community" in text and "마이그레이션 티켓" in text
    org["frontend_guide"] = None
    assert "Tech Lead 참고" not in discover_prompt(org, p, [], 2, "digest")


def test_repo_override_inherits_or_disables_guide(node_python_repo: Path, tmp_path: Path):
    p = detect_profile(node_python_repo)
    base = load_template()
    minimal = {k: base[k] for k in ("roles", "stages", "review_loop")}
    minimal["project"] = {"name": "custom", "description": "d", "conventions": []}
    override = p.org_override
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(yaml.safe_dump(minimal, allow_unicode=True), encoding="utf-8")
    org = load_org_for(p)
    assert org["project"]["name"] == "custom" and org["frontend_guide"] == base["frontend_guide"]   # 상속

    minimal["frontend_guide"] = False
    override.write_text(yaml.safe_dump(minimal, allow_unicode=True), encoding="utf-8")
    assert load_org_for(p)["frontend_guide"] is None                                                 # 끔

    minimal["frontend_guide"] = {"title": "custom guide", "stack": [{"area": "표", "use": "AG Grid Community", "rule": "r"}]}
    override.write_text(yaml.safe_dump(minimal, allow_unicode=True), encoding="utf-8")
    assert "custom guide" in prompts.render_frontend_guide(load_org_for(p)["frontend_guide"])         # 레포 자체 정의
