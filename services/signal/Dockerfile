FROM ghcr.io/helios-plat/helios/python-ml-base:0.1.0
USER root
# hmmlearn has no Python 3.14 wheel — needs g++ to compile Cython extensions
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY services/signal/requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*
# Private quant primitives oprim (vendored, same pattern as sel_v2/Dockerfile): the
# regime detector (oprim.atr) and HMM detector (pearson_spearman_corr) lazy-import it —
# a latent crash that only surfaced once market.candles gained a producer
# (2026-07-10, market-scanner). chardet/pyyaml/statsmodels satisfy oprim's eager imports.
RUN uv pip install --system --no-cache chardet pyyaml statsmodels
COPY vendor/oprim /usr/local/lib/python3.14/site-packages/oprim
COPY --chown=app:app . .
USER app
CMD ["python", "-m", "services.signal.main"]
