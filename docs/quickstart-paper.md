# PAPER quickstart

1. Copy `.env.example` to `.env`. Leave `EXEC_MODE=PAPER`.
2. Do not set `AUTO_EXEC` or `I_HAVE_OOS_EVIDENCE`.
3. `python -m shared.runtime.release_identity --health` should print `funds_scope=paper`.
4. Gateway writes need `X-API-Key`, `X-Request-Id`, `X-Actor`, `X-Timestamp`, `X-Reason`.
5. Without Helios postgres/redis, do not expect compose-up to go green.
