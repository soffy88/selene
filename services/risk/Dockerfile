FROM ghcr.io/helios-plat/helios/python-api-base:0.1.0
USER root
COPY services/risk/requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache -r /tmp/requirements.txt && rm /tmp/requirements.txt
# oprim vendored (same as services/signal): portfolio/var_engine.py lazy-imports
# oprim.value_at_risk — latent ModuleNotFoundError on the VaR gate path once the
# v4 chain gained live signals (2026-07-10). numpy+deps for its eager imports.
RUN uv pip install --system --no-cache numpy chardet pyyaml statsmodels pydantic
COPY vendor/oprim /usr/local/lib/python3.14/site-packages/oprim
COPY --chown=app:app . .
USER app
CMD ["python", "-m", "services.risk.main"]
