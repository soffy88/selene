FROM ghcr.io/helios-plat/helios/python-worker-base:0.1.0
USER root
COPY services/scanner/requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --chown=app:app . .
USER app
CMD ["python", "-m", "services.scanner.main"]
