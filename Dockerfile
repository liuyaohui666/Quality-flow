FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 quality-flow \
    && useradd --uid 10001 --gid quality-flow --create-home --shell /usr/sbin/nologin quality-flow

WORKDIR /app

COPY --chown=quality-flow:quality-flow . /app

RUN pip install --no-cache-dir ".[dev]" \
    && mkdir -p /runtime/workspaces /runtime/staging /runtime/artifacts \
    && chown -R quality-flow:quality-flow /runtime

USER quality-flow

CMD ["uvicorn", "quality_flow.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
