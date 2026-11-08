"""Hermes 모델 연결(OpenAI 호환 / Ollama) 및 엔드포인트 점검."""
from __future__ import annotations

import errno
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .settings import Settings

_NO_ROUTE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL}


def build_llm(settings: Settings):
    from crewai import LLM  # noqa: WPS433

    if settings.provider == "ollama":
        return LLM(model=settings.litellm_model, base_url=settings.base_url, temperature=settings.temperature)
    return LLM(model=settings.litellm_model, base_url=settings.base_url, api_key=settings.api_key, temperature=settings.temperature)


def _hint(exc: OSError) -> str:
    if exc.errno == errno.ECONNREFUSED:
        return "포트에 서버가 없음(Ollama 미실행?)"
    if isinstance(exc, socket.timeout) or exc.errno == errno.ETIMEDOUT:
        return "응답 없음(방화벽/주소 확인)"
    if exc.errno in _NO_ROUTE:
        return "경로 없음(네트워크/IPv6 확인)"
    return ""


def diagnose_connect(url: str, timeout: float = 3.0) -> str | None:
    """URLError 뒤에 주소별로 TCP 연결을 다시 시도해 실제 원인을 가려낸다. 연결이 되면 None.

    host.docker.internal 처럼 IPv4 + IPv6 두 주소로 풀리는 호스트는 urllib 이 마지막에 시도한(IPv6) 오류만 돌려준다.
    컨테이너에 IPv6 경로가 없으면 진짜 원인(IPv4 연결 거부 = 서버 미실행)이 '[Errno 101] Network is unreachable' 로 가려지므로
    IPv4 를 먼저, 경로 없음(ENETUNREACH 등)은 뒤로 보내 가장 유의미한 오류를 고른다.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return f"DNS 조회 실패 {host}: {exc}"
    infos.sort(key=lambda i: i[0] != socket.AF_INET)  # IPv4 먼저
    failures: list[tuple[int, str, OSError]] = []
    for family, _kind, _proto, _canon, addr in infos:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(addr)
            return None  # TCP 연결은 됨 → 원인은 HTTP 단계
        except OSError as exc:
            failures.append((family, str(addr[0]), exc))
    if not failures:
        return None
    failures.sort(key=lambda f: (f[0] != socket.AF_INET, f[2].errno in _NO_ROUTE))
    family, ip, exc = failures[0]
    shown = f"[{ip}]:{port}" if family == socket.AF_INET6 else f"{ip}:{port}"
    msg = f"{exc} ({shown})"
    hint = _hint(exc)
    if hint:
        msg += f" — {hint}"
    others = [f"{ip2}: {exc2}" for fam2, ip2, exc2 in failures[1:]]
    if others:
        msg += f" · 그 외 주소: {'; '.join(others)}"
    return msg


def check_endpoint(settings: Settings, timeout: float = 5.0) -> dict:
    url = f"{settings.openai_compatible_base}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {settings.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "url": url, "error": f"HTTP {exc.code} {exc.reason}", "models": []}
    except urllib.error.URLError as exc:
        error = diagnose_connect(url, timeout=min(timeout, 3.0)) or str(exc.reason)
        return {"ok": False, "url": url, "error": error, "models": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc), "models": []}
    models = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
    return {"ok": True, "url": url, "models": models, "model_available": settings.model in models or not models}
