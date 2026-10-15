# Warden run console (E3): a read-only FastAPI server over the committed site/.
# Build from the repo root (the context needs console/ and site/):
#   docker build --platform linux/amd64 -f console/Dockerfile -t warden-console .
#   docker run --rm -p 8080:8080 warden-console   →  http://localhost:8080/
# Deployed as a Nebius Serverless AI endpoint by ops/deploy-console.sh.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Only the server's two dependencies; the warden CLI is not installed in the image.
RUN pip install "fastapi==0.141.1" "uvicorn==0.53.0" \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin console

WORKDIR /app
COPY --chown=root:root console/__init__.py console/__main__.py console/app.py console/
COPY --chown=root:root site/ site/
ARG BUILD=dev
RUN echo "$BUILD" > console/BUILD

USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
CMD ["python", "-m", "uvicorn", "console.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
