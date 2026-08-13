# Multi-stage: build wheels once, ship a slim runtime image with no toolchain.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Dependency layer first: it changes far less often than the source, so a code
# edit does not invalidate the (slow) dependency install.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --prefix=/install .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.12/site-packages"

# Run unprivileged. Nothing in this service needs root.
RUN useradd --create-home --uid 10001 aic

COPY --from=builder /install /install
COPY --chown=aic:aic migrations /app/migrations
COPY --chown=aic:aic docs/runbooks /app/docs/runbooks

WORKDIR /app
USER aic

EXPOSE 8000

# The readiness endpoint checks the LLM client, the tool registry, and that the
# runbook corpus actually indexed — a liveness ping would not catch a bad mount.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/ready', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "aic.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--log-config", "/dev/null"]
