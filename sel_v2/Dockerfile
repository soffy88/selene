FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir asyncpg aiohttp requests
COPY . /app
ENV PYTHONPATH=/app
