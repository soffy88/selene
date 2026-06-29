FROM python:3.11-slim
WORKDIR /app
# Collectors need only asyncpg/websockets. The paper strategy engine additionally
# requires the scientific stack (pandas/numpy/scipy) and TDA/wavelet libs.
RUN pip install --no-cache-dir \
    asyncpg aiohttp requests websockets websockets_proxy redis \
    pandas numpy scipy ripser PyWavelets
#
# REQUIRED for the paper engine to run the strategies: the private quant primitives
# oprim (Layer-1) and oskill (Layer-2). They are NOT on a public index and were not
# reachable from this build environment. Install them here from the private index, e.g.:
#   ARG PRIVATE_INDEX_URL
#   RUN pip install --no-cache-dir --index-url "$PRIVATE_INDEX_URL" oprim oskill
# Without them the collectors/aggregator still work, but the paper engine will fail
# at reprocess (cusum/hawkes call oprim.percentile_value). NOTE: a same-named public
# "oprim" exists that is an unrelated LLM/edu library — do not install that one.
COPY . /app
ENV PYTHONPATH=/app
