FROM ghcr.io/helios-plat/helios/python-ml-base:0.1.0
USER root
# hmmlearn has no Python 3.14 wheel — needs g++ to compile Cython extensions
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY services/signal/requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*
COPY --chown=app:app . .
USER app
CMD ["python", "-m", "services.signal.main"]
