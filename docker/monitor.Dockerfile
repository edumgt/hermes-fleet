# Hermes Fleet 웹 모니터 (읽기 전용). crewai 는 설치하지 않는다 — repos/backlog/schedule/merge/llm 모듈만 쓴다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul \
    HERMES_FLEET_DIR=/home/ubuntu/hermes-fleet \
    OLLAMA_HOST=host.docker.internal:11434

RUN apt-get update \
 && apt-get install -y --no-install-recommends git tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*'

WORKDIR /app
RUN pip install "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" "pyyaml>=6.0,<7"

COPY hermes_fleet ./hermes_fleet
COPY templates ./templates

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "hermes_fleet.monitor", "--host", "0.0.0.0", "--port", "8787"]
