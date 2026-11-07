"""환경변수 기반 설정과 fleet 경로. .env(hermes-fleet/.env)가 있으면 먼저 읽는다."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

FLEET_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = FLEET_DIR / "state"
LOG_DIR = FLEET_DIR / "logs"
TEMPLATE_ORG = FLEET_DIR / "templates" / "org.yaml"
DEFAULT_FLEET_CONFIG = FLEET_DIR / "fleet.yaml"
PAUSE_FILE = STATE_DIR / "PAUSE"

# 각 저장소 안에서 Hermes가 쓰는 디렉터리 (webrtc-app/hermes 와 같은 규약)
REPO_HERMES_DIR = "hermes"
REPO_BACKLOG_DIR = "hermes/backlog"
REPO_WORKSPACE_DIR = "hermes/workspace"
REPO_ORG_OVERRIDE = "hermes/config/org.yaml"


def load_dotenv(path: Path = FLEET_DIR / ".env") -> None:
    """의존성 없이 KEY=VALUE 형식의 .env를 os.environ에 주입(기존 값 우선, 빈 값은 무시)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key, value)


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def default_gateway() -> str | None:
    try:
        out = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["default"] and "via" in parts:
            return parts[parts.index("via") + 1]
    return None


def detect_ollama_host() -> str:
    """OLLAMA_HOST → WSL이면 Windows 호스트(기본 게이트웨이) → localhost. 실행 시점에 계산해 게이트웨이 IP 변경에 대응."""
    env = os.environ.get("OLLAMA_HOST", "").strip()
    if env:
        return env
    if is_wsl():
        gw = default_gateway()
        if gw:
            return f"{gw}:11434"
    return "localhost:11434"


@dataclass
class Settings:
    provider: str = "ollama"
    model: str = "hermes3:8b"
    base_url: str = "http://localhost:11434"
    api_key: str = "NA"
    temperature: float = 0.2
    max_iterations: int = 2
    shell_timeout: int = 900

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")
        os.environ.setdefault("OTEL_SDK_DISABLED", "true")
        provider = os.environ.get("HERMES_LLM_PROVIDER", "ollama").lower()
        base_url = os.environ.get("HERMES_BASE_URL", "").strip()
        if not base_url and provider == "ollama":
            host = detect_ollama_host()
            base_url = host if "://" in host else f"http://{host}"
        return cls(
            provider=provider,
            model=os.environ.get("HERMES_MODEL", "hermes3:8b"),
            base_url=(base_url or "http://localhost:11434").rstrip("/"),
            api_key=os.environ.get("HERMES_API_KEY", "NA"),
            temperature=float(os.environ.get("HERMES_TEMPERATURE", "0.2")),
            max_iterations=int(os.environ.get("HERMES_MAX_ITERATIONS", "2")),
            shell_timeout=int(os.environ.get("HERMES_SHELL_TIMEOUT", "900")),
        )

    @property
    def litellm_model(self) -> str:
        return f"ollama/{self.model}" if self.provider == "ollama" else f"openai/{self.model}"

    @property
    def openai_compatible_base(self) -> str:
        return f"{self.base_url}/v1" if self.provider == "ollama" else self.base_url


def fleet_config_path() -> Path:
    load_dotenv()
    env = os.environ.get("HERMES_FLEET_CONFIG", "").strip()
    return Path(env).expanduser() if env else DEFAULT_FLEET_CONFIG
