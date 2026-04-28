FROM ghcr.io/helios-plat/helios/python-api-base:0.1.0
# Override base-image healthcheck (EC-01): bar runner has no HTTP server.
HEALTHCHECK NONE

USER root
COPY services/sel_bar_runner/requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --chown=app:app . .
USER app

CMD ["python", "-m", "services.sel_bar_runner.main"]
