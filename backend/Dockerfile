FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
RUN pip install . && useradd --system --uid 10001 appuser
USER appuser

EXPOSE 8080
# --no-access-log: the app logs method/path/status only (no query strings).
CMD ["sh", "-c", "exec uvicorn app.main:create_app_from_env --factory --host 0.0.0.0 --port ${PORT} --no-access-log --timeout-graceful-shutdown 10"]
