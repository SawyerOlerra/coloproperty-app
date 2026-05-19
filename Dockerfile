# Official Playwright image — Chromium + all system deps pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

EXPOSE 8080
# Use shell form so $PORT expands (Railway injects PORT at runtime)
CMD gunicorn --bind 0.0.0.0:${PORT:-8080} --timeout 120 --workers 1 app:app
