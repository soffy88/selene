FROM python:3.11-slim
WORKDIR /app
RUN pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple --no-cache-dir asyncpg aiohttp redis
COPY main.py /app/main.py
CMD ["python", "-u", "/app/main.py"]
