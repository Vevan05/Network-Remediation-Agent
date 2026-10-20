FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask
# Stretch goal (real network faults with tc/netem): uncomment, and keep cap_add: NET_ADMIN in compose.
# RUN apt-get update && apt-get install -y --no-install-recommends iproute2 iptables && rm -rf /var/lib/apt/lists/*
COPY app.py .
EXPOSE 8000
CMD ["python", "app.py"]
