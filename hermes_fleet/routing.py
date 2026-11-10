"""실행 직전의 로컬/외부 생성 LLM 라우팅.

Jev는 타입 있는 레포 판정용 외부 Decisions API로 그대로 둔다. 이 모듈은
실제 크루의 생성 작업만 호스트 GPU 여유도에 따라 Ollama 또는 외부 OpenAI
호환 엔드포인트로 고른다.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from typing import Any, Callable

from .settings import Settings, detect_ollama_host

EndpointCheck = Callable[[Settings], dict[str, Any]]


@dataclass(frozen=True)
class RouteDecision:
    settings: Settings
    route: str                 # local | external | fixed
    reason: str
    gpu_utilization: float | None = None
    gpu_memory: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"route": self.route, "provider": self.settings.provider, "model": self.settings.model,
                "base_url": self.settings.base_url, "reason": self.reason,
                "gpu_utilization": self.gpu_utilization, "gpu_memory": self.gpu_memory}


@dataclass(frozen=True)
class ModelRouter:
    """GPU 여유도 기반 생성 모델 선택기. 환경변수만으로 동작을 바꾼다."""
    mode: str
    fixed: Settings
    local: Settings
    external: Settings
    gpu_max_percent: float = 70.0
    gpu_memory_max_percent: float = 75.0

    @classmethod
    def from_env(cls, default: Settings) -> "ModelRouter":
        mode = os.environ.get("HERMES_ROUTING", "off").strip().lower()
        if mode not in {"off", "auto", "local", "external"}:
            mode = "off"
        local_base = os.environ.get("HERMES_LOCAL_BASE_URL", "").strip()
        if not local_base:
            host = detect_ollama_host()
            local_base = host if "://" in host else f"http://{host}"
        local = replace(default,
                        provider=os.environ.get("HERMES_LOCAL_PROVIDER", "ollama").strip().lower() or "ollama",
                        model=os.environ.get("HERMES_LOCAL_MODEL", "qwen2.5-coder:7b").strip() or "qwen2.5-coder:7b",
                        base_url=local_base.rstrip("/"), api_key=os.environ.get("HERMES_LOCAL_API_KEY", "NA").strip() or "NA",
                        use_role_overrides=False)
        external_provider = os.environ.get("HERMES_EXTERNAL_PROVIDER", default.provider).strip().lower() or default.provider
        external = replace(default,
                           provider=external_provider,
                           model=os.environ.get("HERMES_EXTERNAL_MODEL", default.model).strip() or default.model,
                           base_url=(os.environ.get("HERMES_EXTERNAL_BASE_URL", default.base_url).strip() or default.base_url).rstrip("/"),
                           api_key=(os.environ.get("HERMES_EXTERNAL_API_KEY", default.api_key).strip() or default.api_key),
                           use_role_overrides=False)
        return cls(mode=mode, fixed=default, local=local, external=external,
                   gpu_max_percent=float(os.environ.get("HERMES_ROUTING_GPU_MAX_PERCENT", "70")),
                   gpu_memory_max_percent=float(os.environ.get("HERMES_ROUTING_GPU_MEMORY_MAX_PERCENT", "75")))

    def choose(self, endpoint_check: EndpointCheck) -> RouteDecision:
        if self.mode == "off":
            return RouteDecision(self.fixed, "fixed", "동적 라우팅이 꺼져 있음")
        if self.mode == "external":
            return RouteDecision(self.external, "external", "외부 모델 강제 설정")

        metrics = gpu_metrics()
        if self.mode == "local":
            return self._local_or_external(endpoint_check, metrics, "내부 모델 강제 설정")
        if metrics is None:
            return RouteDecision(self.external, "external", "GPU 상태를 읽을 수 없어 외부 모델 선택")
        util, memory = metrics
        if util >= self.gpu_max_percent or memory >= self.gpu_memory_max_percent:
            return RouteDecision(self.external, "external",
                                 f"GPU {util:.0f}% / VRAM {memory:.0f}% (한계 {self.gpu_max_percent:.0f}% / {self.gpu_memory_max_percent:.0f}%)",
                                 util, memory)
        return self._local_or_external(endpoint_check, metrics,
                                       f"GPU {util:.0f}% / VRAM {memory:.0f}% 여유",)

    def _local_or_external(self, endpoint_check: EndpointCheck, metrics: tuple[float, float] | None, reason: str) -> RouteDecision:
        util, memory = metrics if metrics is not None else (None, None)
        check = endpoint_check(self.local)
        if check.get("ok"):
            return RouteDecision(self.local, "local", reason, util, memory)
        return RouteDecision(self.external, "external", f"{reason}; 내부 엔드포인트 불가 → 외부 폴백 ({check.get('error', 'unknown error')})", util, memory)


def gpu_metrics() -> tuple[float, float] | None:
    """가장 여유 있는 NVIDIA GPU의 (사용률, VRAM 사용률)를 반환한다."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    try:
        rows = [[float(v.strip()) for v in line.split(",")] for line in result.stdout.splitlines() if line.strip()]
        candidates = [(row[0], 100 * row[1] / row[2]) for row in rows if len(row) == 3 and row[2] > 0]
        return min(candidates, key=lambda row: (row[1], row[0])) if candidates else None
    except (ValueError, IndexError, ZeroDivisionError):
        return None
