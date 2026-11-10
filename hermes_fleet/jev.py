"""Jev(TypeSafe AI 'System One' 판단 전용 모델) 클라이언트 — 의존성 없이 urllib 만 쓴다(모니터 컨테이너에도 crewai 가 없다).

Jev 는 문장을 생성하지 않고 state(텍스트/JSON) + 타입 있는 질문을 받아 '선택지·확률·confidence' 만 돌려준다.
  - choice: criteria(옵션 → 설명) 중 하나 고름 → {choice, probabilities, confidence}
  - score : criteria(낮음→높음 단계 설명 배열) 위의 확률 가중 점수 → {score, legend, probabilities, confidence}
  - noul  : 예/아니오 진술이 참일 확률 → {noul: 0~1}
계약: POST {base}/v1/systemone, Authorization: Bearer <TYPESAFE_API_KEY>, 응답 {model, answers{key→answer}, usage{input_tokens,output_tokens}}.
오류: 401(키) 422(검증: detail 목록) 429(속도 제한) 529(과부하) — 429/529/408/5xx 는 지수 백오프로 재시도(Retry-After 존중).
TypeSafe 직접 API와 OpenRouter Decisions API를 모두 지원한다. OpenRouter는 일반 채팅 API가 아니라
`POST /api/alpha/decisions`를 써야 하며, `typesafe/jev-*` 모델 ID를 사용한다.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .settings import load_dotenv

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
SYSTEM_ONE_PATH = "/v1/systemone"
MODELS_PATH = "/v1/models"
OPENROUTER_BASE_URL = "https://openrouter.ai"
OPENROUTER_DECISIONS_PATH = "/api/alpha/decisions"
OPENROUTER_MODELS_PATH = "/api/v1/models"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 529}
USER_AGENT = "hermes-fleet/0.1 (+jev review)"

Http = Callable[[urllib.request.Request, float], tuple[int, bytes, dict[str, str]]]


class JevError(Exception):
    """API 호출 실패. status 는 HTTP 상태(네트워크 오류면 None), detail 은 서버가 준 설명."""

    def __init__(self, message: str, status: int | None = None, detail: Any = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.request_id = request_id


@dataclass
class JevSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    provider: str = "typesafe"
    timeout: float = 30.0
    retries: int = 3

    @classmethod
    def from_env(cls, model: str | None = None) -> "JevSettings":
        """환경변수/.env에서 TypeSafe 또는 OpenRouter Jev 설정을 읽는다.

        `JEV_PROVIDER=openrouter`를 명시할 수 있고, 생략 시 OpenRouter URL·키 접두사로
        추론한다. OpenRouter 키는 `OPENROUTER_API_KEY`를 우선하며 기존
        `TYPESAFE_API_KEY` 설정도 호환한다.
        """
        load_dotenv()
        explicit_provider = os.environ.get("JEV_PROVIDER", "").strip().lower()
        configured_base = os.environ.get("TYPESAFE_BASE_URL", "").strip().rstrip("/")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        legacy_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        provider = explicit_provider or (
            "openrouter" if "openrouter.ai" in configured_base or openrouter_key or legacy_key.startswith("sk-or-") else "typesafe"
        )
        if provider not in {"typesafe", "openrouter"}:
            provider = "typesafe"
        api_key = openrouter_key if provider == "openrouter" and openrouter_key else legacy_key
        base_url = configured_base or (OPENROUTER_BASE_URL if provider == "openrouter" else DEFAULT_BASE_URL)
        env_model = os.environ.get("JEV_MODEL", "").strip()
        default_model = "typesafe/jev-1.14" if provider == "openrouter" else DEFAULT_MODEL
        return cls(api_key=api_key, base_url=base_url, model=(model or env_model or default_model), provider=provider)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}

    @property
    def decisions_path(self) -> str:
        return OPENROUTER_DECISIONS_PATH if self.provider == "openrouter" else SYSTEM_ONE_PATH

    @property
    def models_path(self) -> str:
        return OPENROUTER_MODELS_PATH if self.provider == "openrouter" else MODELS_PATH


def _http(req: urllib.request.Request, timeout: float) -> tuple[int, bytes, dict[str, str]]:
    """(status, body, headers). HTTPError 도 예외 대신 상태 코드로 돌려준다(테스트에서 바꿔 끼운다)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}


def _parse(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")[:500]


def _detail_text(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, list):
            return "; ".join(f"{'.'.join(str(x) for x in d.get('loc', []))}: {d.get('msg')}" if isinstance(d, dict) else str(d) for d in detail)
        return str(detail)
    return str(payload)


def _request(settings: JevSettings, method: str, path: str, body: dict[str, Any] | None, http: Http | None, sleep: Callable[[float], None]) -> Any:
    if not settings.configured:
        raise JevError("TYPESAFE_API_KEY 가 없습니다 (hermes-fleet/.env 또는 환경변수). 키 발급: https://console.typesafe.ai/keys")
    url = settings.base_url + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    call = http or _http
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=settings.headers(), method=method)
        try:
            status, raw, headers = call(req, settings.timeout)
        except urllib.error.URLError as exc:
            if attempt >= settings.retries:
                raise JevError(f"Jev 연결 실패 {url}: {exc.reason}", status=None) from exc
            attempt += 1
            sleep(min(2.0 ** attempt, 20.0))
            continue
        except OSError as exc:  # timeout 등
            if attempt >= settings.retries:
                raise JevError(f"Jev 연결 실패 {url}: {exc}", status=None) from exc
            attempt += 1
            sleep(min(2.0 ** attempt, 20.0))
            continue
        payload = _parse(raw)
        request_id = headers.get("x-typesafe-request-id")
        if status == 200:
            return payload
        if status in RETRY_STATUSES and attempt < settings.retries:
            attempt += 1
            retry_after = headers.get("retry-after")
            try:
                delay = float(retry_after) if retry_after else min(2.0 ** attempt, 20.0)
            except ValueError:
                delay = min(2.0 ** attempt, 20.0)
            sleep(max(0.0, min(delay, 60.0)))
            continue
        hint = {401: "API 키가 없거나 잘못됨", 422: "요청 검증 실패", 429: "속도 제한", 529: "서버 과부하", 400: "요청 규칙 위반(컨텍스트 초과 등)"}.get(status, "")
        raise JevError(f"Jev HTTP {status}{' ' + hint if hint else ''}: {_detail_text(payload)}", status=status, detail=payload, request_id=request_id)


def decide(state: Any, questions: dict[str, dict[str, Any]], settings: JevSettings, http: Http | None = None,
           sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """한 번의 System One 호출. 응답 {model, answers, usage} 를 그대로 돌려준다(answers 가 없으면 JevError)."""
    if not questions:
        raise JevError("질문이 비어 있습니다")
    payload = _request(settings, "POST", settings.decisions_path, {"model": settings.model, "state": state, "questions": questions}, http, sleep)
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        raise JevError(f"Jev 응답 형식이 다릅니다: {str(payload)[:300]}", status=200, detail=payload)
    missing = [k for k in questions if k not in payload["answers"]]
    if missing:
        raise JevError(f"Jev 응답에 질문 {missing} 의 답이 없습니다", status=200, detail=payload)
    return payload


def check(settings: JevSettings, http: Http | None = None) -> dict[str, Any]:
    """GET /v1/models 로 키·연결을 점검한다. {ok, url, model, models, error}."""
    url = settings.base_url + settings.models_path
    if not settings.configured:
        return {"ok": False, "url": url, "model": settings.model, "models": [], "error": "TYPESAFE_API_KEY 없음"}
    try:
        payload = _request(settings, "GET", settings.models_path, None, http, lambda s: None)
    except JevError as exc:
        return {"ok": False, "url": url, "model": settings.model, "models": [], "error": str(exc)}
    names: list[str] = []
    rows = payload.get("models") or payload.get("data") or [] if isinstance(payload, dict) else payload
    for m in rows or []:
        if isinstance(m, dict):
            # OpenRouter는 `name`에 표시명("TypeSafe: Jev 1.14")을, `id`에
            # 실제 호출 모델 ID("typesafe/jev-1.14")를 담는다. 둘 다 보존해야
            # model_available 검사와 UI 목록이 정확하다.
            names.extend(str(m.get(k) or "") for k in ("id", "name"))
            for alias in m.get("aliases") or []:
                names.append(str(alias))
        elif isinstance(m, str):
            names.append(m)
    names = [n for n in names if n]
    # OpenRouter의 /models 카탈로그는 Decisions API 전용 Jev 버전/별칭을 항상
    # 모두 열거하지 않는다. `typesafe/jev-*`는 Decisions 호출에서 판정하므로
    # 카탈로그 누락만으로 경고하지 않는다(실제 호출 오류는 review 결과에 남는다).
    openrouter_jev = settings.provider == "openrouter" and settings.model.lstrip("~").startswith("typesafe/jev-")
    return {"ok": True, "url": url, "model": settings.model, "models": names,
            "model_available": settings.model in names or not names or openrouter_jev, "error": None}


# ---------- 답 읽기 헬퍼 ----------

def choice_of(answer: dict[str, Any]) -> tuple[str, float, dict[str, float]]:
    """(선택지, confidence, 옵션별 확률)."""
    probs = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
    choice = str(answer.get("choice") or (max(probs, key=probs.get) if probs else ""))
    return choice, float(answer.get("confidence") or 0.0), probs


def score_of(answer: dict[str, Any]) -> tuple[float, str, float]:
    """(확률 가중 점수, 가장 가까운 단계 설명, confidence)."""
    score = float(answer.get("score") or 0.0)
    legend = {str(k): str(v) for k, v in (answer.get("legend") or {}).items()}
    label = legend.get(str(int(round(score))), "")
    return score, label, float(answer.get("confidence") or 0.0)


def noul_of(answer: dict[str, Any]) -> float:
    return float(answer.get("noul") or 0.0)
