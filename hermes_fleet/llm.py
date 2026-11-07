"""Hermes 모델 연결(OpenAI 호환 / Ollama) 및 엔드포인트 점검."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .settings import Settings


def build_llm(settings: Settings):
    from crewai import LLM  # noqa: WPS433

    if settings.provider == "ollama":
        return LLM(model=settings.litellm_model, base_url=settings.base_url, temperature=settings.temperature)
    return LLM(model=settings.litellm_model, base_url=settings.base_url, api_key=settings.api_key, temperature=settings.temperature)


def check_endpoint(settings: Settings, timeout: float = 5.0) -> dict:
    url = f"{settings.openai_compatible_base}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {settings.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {"ok": False, "url": url, "error": str(exc.reason), "models": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc), "models": []}
    models = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
    return {"ok": True, "url": url, "models": models, "model_available": settings.model in models or not models}
