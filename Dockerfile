FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

EXPOSE 8080
COPY start.sh .
RUN chmod +x start.sh
CMD ["/bin/bash", "start.sh"]
