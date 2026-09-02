FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gcc g++ git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-qualification.txt /tmp/requirements-qualification.txt
RUN pip install --no-cache-dir -r /tmp/requirements-qualification.txt \
    && pip install --no-cache-dir --no-deps \
        "oprim @ git+https://github.com/helios-plat/oprim@v3.10.33" \
        "oskill @ git+https://github.com/helios-plat/oskill@v4.3.0"
COPY . /app
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app
CMD ["python", "-m", "services.scanner.main"]
