FROM python:3.11-slim
WORKDIR /app
COPY worker.py /app/worker.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-u", "/app/worker.py"]
