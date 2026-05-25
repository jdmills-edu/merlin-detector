FROM python:3.11-slim

WORKDIR /opt/merlin-mcp

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PYTHONUNBUFFERED=1 \
    DETECTIONS_DB=/data/detections.sqlite \
    TZ=America/New_York

EXPOSE 8080

CMD ["python", "server.py"]
