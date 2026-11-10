"""Jev(TypeSafe System One) 클라이언트: 요청 형식·응답 파싱·오류·재시도 (네트워크 없이 _http 를 바꿔 끼운다)."""
import json
import urllib.error

import pytest

from hermes_fleet import jev


def ok_answers():
    return {"model": "jev-1.14.0", "answers": {
        "proceed": {"type": "choice", "choice": "proceed", "confidence": 0.82, "probabilities": {"proceed": 0.9, "skip": 0.1}},
        "activity": {"type": "score", "score": 2.7, "confidence": 0.7, "legend": {"0": "a", "1": "b", "2": "c", "3": "d"}, "probabilities": {"0": 0, "1": 0, "2": 0.3, "3": 0.7}},
        "changed": {"type": "noul", "noul": 0.15}}, "usage": {"input_tokens": 300, "output_tokens": 12}}


def test_decide_sends_contract_and_parses(monkeypatch):
    seen = {}

    def fake_http(req, timeout):
        seen["url"], seen["method"], seen["headers"], seen["body"] = req.full_url, req.get_method(), dict(req.header_items()), json.loads(req.data)
        return 200, json.dumps(ok_answers()).encode(), {"x-typesafe-request-id": "r1"}

    st = jev.JevSettings(api_key="k", model="jev-latest")
    qs = {"proceed": {"type": "choice", "instructions": "x", "criteria": {"proceed": "a", "skip": "b"}}, "activity": {"type": "score", "criteria": ["a", "b"]}, "changed": {"type": "noul", "instructions": "y"}}
    res = jev.decide({"repository": "shop"}, qs, st, http=fake_http)
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone" and seen["method"] == "POST"
    assert seen["headers"]["Authorization"] == "Bearer k" and seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == {"model": "jev-latest", "state": {"repository": "shop"}, "questions": qs}
    assert res["model"] == "jev-1.14.0"
    assert jev.choice_of(res["answers"]["proceed"]) == ("proceed", 0.82, {"proceed": 0.9, "skip": 0.1})
    score, label, conf = jev.score_of(res["answers"]["activity"])
    assert (score, label, conf) == (2.7, "d", 0.7)
    assert jev.noul_of(res["answers"]["changed"]) == 0.15


def test_missing_key_and_http_errors():
    with pytest.raises(jev.JevError, match="TYPESAFE_API_KEY"):
        jev.decide("s", {"q": {"type": "noul", "instructions": "x"}}, jev.JevSettings(api_key=""), http=lambda r, t: (200, b"{}", {}))
    st = jev.JevSettings(api_key="k", retries=0)
    with pytest.raises(jev.JevError) as ei:
        jev.decide("s", {"q": {"type": "noul", "instructions": "x"}}, st, http=lambda r, t: (401, b'{"detail":"Invalid API key"}', {}))
    assert ei.value.status == 401 and "Invalid API key" in str(ei.value)
    body = json.dumps({"detail": [{"loc": ["body", "state"], "msg": "Field required", "type": "missing"}]}).encode()
    with pytest.raises(jev.JevError, match="body.state: Field required"):
        jev.decide("s", {"q": {"type": "noul", "instructions": "x"}}, st, http=lambda r, t: (422, body, {}))
    # answers 누락
    with pytest.raises(jev.JevError, match="답이 없습니다"):
        jev.decide("s", {"q": {"type": "noul", "instructions": "x"}}, st, http=lambda r, t: (200, b'{"answers": {}}', {}))


def test_retries_on_429_and_network_then_succeeds():
    calls = []
    sleeps = []

    def flaky(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("boom")
        if len(calls) == 2:
            return 429, b'{"detail":"slow down"}', {"retry-after": "0.5"}
        if len(calls) == 3:
            return 529, b"", {}
        return 200, json.dumps(ok_answers()).encode(), {}

    st = jev.JevSettings(api_key="k", retries=3)
    res = jev.decide("s", {"proceed": {"type": "choice", "criteria": {"proceed": None, "skip": None}}}, st, http=flaky, sleep=sleeps.append)
    assert res["answers"]["proceed"]["choice"] == "proceed" and len(calls) == 4
    assert sleeps[1] == 0.5          # Retry-After 존중
    # 재시도 소진 → 마지막 상태로 실패
    st0 = jev.JevSettings(api_key="k", retries=1)
    with pytest.raises(jev.JevError) as ei:
        jev.decide("s", {"q": {"type": "noul", "instructions": "x"}}, st0, http=lambda r, t: (529, b"", {}), sleep=lambda s: None)
    assert ei.value.status == 529


def test_settings_from_env_and_check(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://openrouter.ai/api/")
    monkeypatch.setenv("JEV_MODEL", "typesafe/jev-1.14")
    st = jev.JevSettings.from_env()
    assert not st.configured and st.base_url == "https://openrouter.ai/api" and st.model == "typesafe/jev-1.14"
    assert jev.JevSettings.from_env(model="jev-1.13.0").model == "jev-1.13.0"
    assert jev.check(st)["error"] == "TYPESAFE_API_KEY 없음"
    st2 = jev.JevSettings(api_key="k")
    models = json.dumps({"models": [{"name": "jev-1.14.0", "aliases": ["jev-latest", "jev-preview"]}, {"name": "jev-1.13.0"}]}).encode()
    info = jev.check(st2, http=lambda r, t: (200, models, {}))
    assert info["ok"] and "jev-latest" in info["models"] and info["model_available"]
    assert not jev.check(st2, http=lambda r, t: (401, b'{"detail":"bad"}', {}))["ok"]
