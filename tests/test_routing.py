"""호스트 GPU 여유도에 따른 내부 Ollama / 외부 모델 선택."""
from hermes_fleet import routing
from hermes_fleet.settings import Settings


def test_auto_uses_local_when_gpu_has_headroom(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTING", "auto")
    monkeypatch.setenv("HERMES_LOCAL_MODEL", "local-coder")
    monkeypatch.setattr(routing, "gpu_metrics", lambda: (25.0, 40.0))
    router = routing.ModelRouter.from_env(Settings(provider="openai", model="remote", base_url="https://remote"))
    decision = router.choose(lambda s: {"ok": s.provider == "ollama"})
    assert decision.route == "local" and decision.settings.model == "local-coder"
    assert decision.gpu_utilization == 25.0 and decision.gpu_memory == 40.0


def test_auto_uses_external_when_gpu_busy(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTING", "auto")
    monkeypatch.delenv("HERMES_EXTERNAL_MODEL", raising=False)
    monkeypatch.setattr(routing, "gpu_metrics", lambda: (71.0, 20.0))
    remote = Settings(provider="openai", model="remote", base_url="https://remote")
    decision = routing.ModelRouter.from_env(remote).choose(lambda _s: {"ok": True})
    assert decision.route == "external" and decision.settings.model == remote.model
    assert decision.settings.use_role_overrides is False
    assert "한계" in decision.reason


def test_auto_falls_back_to_external_when_local_unavailable(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTING", "auto")
    monkeypatch.delenv("HERMES_EXTERNAL_MODEL", raising=False)
    monkeypatch.setattr(routing, "gpu_metrics", lambda: (10.0, 10.0))
    remote = Settings(provider="openai", model="remote", base_url="https://remote")
    decision = routing.ModelRouter.from_env(remote).choose(lambda _s: {"ok": False, "error": "down"})
    assert decision.route == "external" and decision.settings.model == remote.model
    assert "외부 폴백" in decision.reason
