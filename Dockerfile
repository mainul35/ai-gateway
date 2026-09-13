FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_FILE=/app/config/config.properties

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY . .

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=5)"

# A single worker process, so /api/cancel reaches the process running the deploy; threads serve concurrent
# requests. No worker timeout, because model downloads stream progress for a long time.
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--timeout", "0", "--bind", "0.0.0.0:5000", "--access-logfile", "-", "app:app"]
