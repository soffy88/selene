FROM python:3.11-slim
WORKDIR /app
# Collectors need only asyncpg/websockets. The paper strategy engine additionally
# requires the scientific stack (pandas/numpy/scipy) + TDA/wavelet libs, and the
# private quant primitives oprim/oskill plus oprim's eager-import deps
# (pyyaml/pydantic/chardet/statsmodels).
RUN pip install --no-cache-dir \
    asyncpg aiohttp requests websockets websockets_proxy redis \
    pandas numpy scipy ripser PyWavelets \
    pyyaml pydantic chardet statsmodels
# Private quant primitives oprim (Layer-1) / oskill (Layer-2). Not on a reachable
# package index, so vendored into the build context (vendor/ is gitignored; populate
# it from the private index or an existing install before building). They are
# lazy-loaded pure-python; the deps above satisfy their eager imports.
COPY vendor/oprim  /usr/local/lib/python3.11/site-packages/oprim
COPY vendor/oskill /usr/local/lib/python3.11/site-packages/oskill
COPY . /app
ENV PYTHONPATH=/app
