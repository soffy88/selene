FROM ghcr.io/helios-plat/helios/python-api-base:0.1.0
USER root
COPY services/execution/requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --chown=app:app . .
USER app
CMD ["python", "-m", "services.execution.main"]
