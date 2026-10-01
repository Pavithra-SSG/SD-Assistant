# One image for every process; docker-compose.yml picks the command:
#   migrate   python manage.py migrate   (one-shot, database owner: schema changes + database accounts)
#   api       uvicorn api:app            (internal network only)
#   worker    python worker.py           (alert timers, sweeps, outbox, retention)
#   ui        streamlit run app.py       (behind the HTTPS proxy)
#   watchdog  python healthwatch.py      (tells IT when the service desk itself is down)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production

WORKDIR /app
# libgl1 + libglib2.0-0: needed by OpenCV, which the on-server screenshot reader (OCR) uses
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Run as an unprivileged user; the code is read-only to it. /data/attachments is the only place it writes
# (a Docker volume), and it is created here so the volume inherits the right owner.
RUN useradd --system --uid 10001 --home-dir /app servicedesk \
    && chown -R root:root /app && chmod -R a+rX /app \
    && mkdir -p /data/attachments && chown servicedesk /data/attachments
USER servicedesk

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    ATTACHMENT_DIR=/data/attachments

EXPOSE 8000 8501
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
