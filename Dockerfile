FROM python:3.12-slim

# Deterministic, unbuffered output so container logs are useful immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so this layer caches across source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools

# Documentation of the port the service listens on. Bind address is 0.0.0.0
# below so the container is reachable from outside.
EXPOSE 8000

# No secrets are baked into this image. LLM_API_KEY must be supplied at run
# time, e.g.  docker run -e LLM_API_KEY=... -p 8000:8000 <image>
ENV PORT=8000 \
    LLM_API_STYLE=anthropic

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
