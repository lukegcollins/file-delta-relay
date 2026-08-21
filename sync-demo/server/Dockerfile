FROM python:3.12-slim
WORKDIR /app
# curl backs the compose healthcheck (python:*-slim does not ship it).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV SYNC_DATA_DIR=/data SYNC_PORT=8000
EXPOSE 8000
CMD ["python", "app.py"]
