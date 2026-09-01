FROM python:3.12-slim
WORKDIR /app
# iproute2 gives the container `tc`, so the network scenarios can shape this
# container's own egress (netem) without touching the host.
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY change_detection.py single_file_transfer.py transport.py ./
# The compose file mounts the demo CA at /certs; any https:// server is
# verified against it. Plain http:// servers ignore the bundle.
ENV SYNC_SERVERS=https://primary-server:8000,https://secondary-server:8001 \
    SYNC_CA_BUNDLE=/certs/ca.crt \
    SYNC_ROOTS=/data/sync \
    SYNC_STATE_DB=/state/client.db \
    SYNC_INTERVAL=3
CMD ["python", "transport.py"]
