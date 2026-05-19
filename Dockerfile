# Official Playwright image — Chromium + all system deps pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

EXPOSE 8080
COPY start.sh .
RUN chmod +x start.sh
CMD ["/bin/bash", "start.sh"]
