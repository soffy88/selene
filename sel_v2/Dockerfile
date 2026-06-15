FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir asyncpg aiohttp requests websockets websockets_proxy redis
COPY . /app
ENV PYTHONPATH=/app
