FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir asyncpg redis pyyaml docker
COPY services/healthcheck/main.py /app/main.py
COPY configs/healthcheck-rules.yaml /app/configs/healthcheck-rules.yaml
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
