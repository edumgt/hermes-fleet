"""check_endpoint 의 오류 진단: IPv4+IPv6 이중 주소에서 IPv6 '경로 없음' 이 실제 원인(연결 거부)을 가리지 않아야 한다."""
import errno
import socket

from hermes_fleet import llm
from hermes_fleet.settings import Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_refused_reports_real_reason_with_hint():
    port = _free_port()
    info = llm.check_endpoint(Settings(provider="ollama", base_url=f"http://127.0.0.1:{port}"), timeout=2.0)
    assert info["ok"] is False
    assert "Connection refused" in info["error"] or "refused" in info["error"].lower()
    assert f"127.0.0.1:{port}" in info["error"]
    assert "Ollama 미실행" in info["error"]


def test_dual_stack_prefers_ipv4_error_over_ipv6_no_route(monkeypatch):
    """컨테이너의 host.docker.internal 상황 재현: IPv4 는 연결 거부, IPv6 는 ENETUNREACH. urllib 은 마지막(IPv6) 오류를 돌려주지만
    진단은 IPv4 연결 거부를 앞에 세우고 IPv6 는 '그 외 주소' 로만 덧붙인다."""
    port = 11434
    infos = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fdc4:f303:9324::254", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", port)),
    ]
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: infos)

    class FakeSock:
        def __init__(self, family, _kind):
            self.family = family

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def settimeout(self, _t):
            pass

        def connect(self, _addr):
            if self.family == socket.AF_INET6:
                raise OSError(errno.ENETUNREACH, "Network is unreachable")
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(llm.socket, "socket", FakeSock)
    msg = llm.diagnose_connect("http://host.docker.internal:11434/v1/models", timeout=1.0)
    assert msg is not None
    assert msg.startswith("[Errno 111] Connection refused (192.168.65.254:11434)")
    assert "Ollama 미실행" in msg
    assert "Network is unreachable" in msg  # IPv6 도 부기록으로 남긴다
    assert msg.index("Connection refused") < msg.index("Network is unreachable")


def test_connectable_returns_none_so_http_reason_is_kept():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert llm.diagnose_connect(f"http://127.0.0.1:{port}/v1/models", timeout=1.0) is None
