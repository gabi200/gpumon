# Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir paho-mqtt

COPY gpumon /app/gpumon

EXPOSE 9109
ENTRYPOINT ["python", "-m", "gpumon.bridge"]
CMD ["--mqtt-host", "mosquitto", "--port", "9109"]
