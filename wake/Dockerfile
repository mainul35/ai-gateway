FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wake_front.py .
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 9100
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9100/__wake/health', timeout=5)"
# One worker: the wake state (who is waiting, which keys the gateway accepted) lives in memory
CMD ["uvicorn", "wake_front:app", "--host", "0.0.0.0", "--port", "9100", "--workers", "1"]
