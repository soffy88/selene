"""
services/notification/main.py  —  CryptoWatch v4 Notification Service 启动入口
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

from shared.db.connections import get_redis, redis_health
from services.notification.hub import NotificationHub, TelegramChannel, DingTalkChannel

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

_hub: NotificationHub = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _hub
    redis_url = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")

    # 使用 shared.db.redis_client（NotificationHub 内部用这个）
    from shared.db.redis_client import init_redis as _init
    _init(redis_url)

    tg = TelegramChannel(
        os.getenv("TELEGRAM_BOT_TOKEN", ""),
        os.getenv("TELEGRAM_CHAT_ID",   ""),
    )
    dd = DingTalkChannel(os.getenv("DINGTALK_WEBHOOK_URL", ""))
    _hub = NotificationHub(tg, dd)

    task = asyncio.create_task(_hub.run())
    logger.info("Notification service ready")
    yield
    task.cancel()
    await tg.close()


app = FastAPI(title="CryptoWatch v4 Notification Service", lifespan=lifespan)

# ── Prometheus metrics (item #12) ───────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    """Prometheus exposition: service liveness + redis reachability.
    Scraped by the central observability stack (Prometheus/Grafana)."""
    from fastapi.responses import PlainTextResponse
    from shared.metrics import render_prometheus
    out = [{"name": "selene_up", "value": 1, "labels": {"service": "notification"},
            "help": "service process is up"}]
    try:
        from shared.db.connections import redis_health
        out.append({"name": "selene_redis_up", "value": await redis_health(),
                    "labels": {"service": "notification"}, "help": "redis reachable"})
    except Exception:
        pass
    return PlainTextResponse(render_prometheus(out))



@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "service": "notification",
        "stats":   _hub.get_stats() if _hub else {},
        "ts":      datetime.utcnow().isoformat(),
    }
