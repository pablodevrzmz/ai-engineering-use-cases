# Session API: FastAPI + uvicorn, built for the same architecture as the
# Fargate task (ARM64). One stage is enough: the image has no build step.
FROM --platform=linux/arm64 python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first so a code change does not invalidate this layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

# Run as an unprivileged user; nothing here needs root.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin api
USER 1000

EXPOSE 8000

# --no-access-log: the load balancer already logs requests, and access lines
# for a long SSE stream are noise. The graceful timeout lets in-flight
# streams finish when a task is replaced.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--timeout-graceful-shutdown", "30"]
