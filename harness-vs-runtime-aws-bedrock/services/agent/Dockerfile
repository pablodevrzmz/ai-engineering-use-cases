# Agent container for Amazon Bedrock AgentCore Runtime (the Runtime flavor).
# AgentCore runs ARM64 images only, hence the explicit platform; build with
#   docker buildx build --platform linux/arm64 --provenance=false -t <repo>:<tag> --push .
FROM --platform=linux/arm64 python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first, so a code change does not re-install them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user; the service needs no files outside its own code.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin agent
USER 1000

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
