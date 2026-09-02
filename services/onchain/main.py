"""
services/onchain/main.py  —  CryptoWatch v4 · Onchain Sentinel

第 20 个服务，完全集成进 cw4 架构：
  - 使用 shared.db.redis_client（与其他服务相同的 Redis 连接）
  - 使用 shared.events.streams（与 cw4 Stream 命名空间一致）
  - 订阅 signal.scored 获取 regime + win_probability
  - 写入 signal.raw 触发 signal-service 重新评分
  - 写入 system.alerts 让 NotificationHub 统一推送
  - 暴露 /health + /api/v4/onchain/* 供 gateway 聚合
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

import aiohttp
from aiohttp import web

from services.onchain.historian import (
    backfill_outcomes,
    get_signal_stats,
    get_wallet_stats,
    record_alert,
)
from services.onchain.scorer import OnchainScorer, RawOnchainEvent
from shared.db.redis_client import get_redis, init_redis
from shared.events.streams import (
    STREAM_SIGNAL_RAW,
    STREAM_SIGNAL_SCORED,
    STREAM_SYSTEM_ALERTS,
    decode,
    encode,
)

logger = logging.getLogger("onchain.main")

# ── 新增 Stream 名称（在 cw4 的命名空间内）────────────
STREAM_ONCHAIN_EVENTS = "onchain.events"  # btc/eth/sol worker 写入
CG_ONCHAIN = "onchain-sentinel"

# 推送阈值
PUSH_STRONG = float(os.environ.get("PUSH_THRESHOLD_STRONG", "0.62"))
PUSH_WATCH = float(os.environ.get("PUSH_THRESHOLD_WATCH", "0.56"))

CHAIN_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


class OnchainService:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._scorers = {sym: OnchainScorer(sym) for sym in CHAIN_SYMBOL.values()}
        self._regimes: dict[str, str] = {}
        self._prices: dict[str, float] = {}
        self._stats = {"events": 0, "pushes": 0, "errors": 0}

    # ── 启动 ──────────────────────────────────────────
    async def start(self):
        redis_url = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")
        init_redis(redis_url)
        self._session = aiohttp.ClientSession()

        r = get_redis()
        for stream in [STREAM_ONCHAIN_EVENTS, STREAM_SIGNAL_SCORED]:
            try:
                await r.xgroup_create(stream, CG_ONCHAIN, id="0", mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise

        logger.info(f"OnchainService started | strong={PUSH_STRONG} watch={PUSH_WATCH}")

        await asyncio.gather(
            self._consume_onchain_events(),
            self._consume_signal_scored(),
            self._price_updater(),
            self._decay_scheduler(),
            self._backfill_scheduler(),
            self._http_server(),
        )

    # ── Consumer: 链上原始事件 ────────────────────────
    async def _consume_onchain_events(self):
        r = get_redis()
        while True:
            try:
                results = await r.xreadgroup(
                    CG_ONCHAIN,
                    "sentinel-events",
                    {STREAM_ONCHAIN_EVENTS: ">"},
                    count=20,
                    block=1000,
                )
                if not results:
                    continue
                for _, messages in results:
                    for msg_id, fields in messages:
                        try:
                            await self._handle_event(decode(fields))
                            await r.xack(STREAM_ONCHAIN_EVENTS, CG_ONCHAIN, msg_id)
                        except Exception as e:
                            logger.error(f"handle_event: {e}", exc_info=True)
                            self._stats["errors"] += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"consume_onchain_events: {e}")
                await asyncio.sleep(1)

    async def _handle_event(self, data: dict):
        event = RawOnchainEvent(
            id=data.get("id", ""),
            chain=data.get("chain", "BTC"),
            symbol=data.get("symbol", "BTCUSDT"),
            event_type=data.get("event_type", "whale_transfer"),
            from_addr=data.get("from_addr", ""),
            to_addr=data.get("to_addr", ""),
            amount=float(data.get("amount", 0)),
            amount_usd=float(data.get("amount_usd", 0)),
            severity=data.get("severity", "medium"),
            meta=data.get("meta", {}),
        )

        sym = CHAIN_SYMBOL.get(event.chain, "BTCUSDT")
        scorer = self._scorers[sym]
        regime = self._regimes.get(sym, "UNKNOWN")

        score = scorer.ingest(event)
        comp = scorer.composite_score()

        # SQLite 历史记录
        record_alert(
            alert_id=event.id,
            symbol=sym,
            chain=event.chain,
            signal_class=event.classify(),
            severity=event.severity,
            amount_usd=event.amount_usd,
            onchain_score=score,
            regime=regime,
            price_at_alert=self._prices.get(sym, 0.0),
            win_prob_cw4=comp.get("cw4_win_prob"),
            fused_prob=comp.get("fused_win_prob"),
        )

        # 写 Redis 状态快照（供 signal-service + gateway 读）
        r = get_redis()
        await r.set(f"onchain:state:{sym}", json.dumps(scorer.get_state()), ex=3600)

        # 写 signal.raw → 触发 signal-service 重新合并 onchain factor
        await r.xadd(
            STREAM_SIGNAL_RAW,
            encode(
                {
                    "source": "onchain-sentinel",
                    "symbol": sym,
                    "factor": "onchain",
                    "factor_score": str(round(comp.get("regime_adj_score", 0), 4)),
                    "net_ex_flow": str(round(comp.get("net_exchange_flow", 0), 2)),
                    "regime": comp.get("regime", "UNKNOWN"),
                    "trigger_id": event.id,
                    "ts": str(time.time()),
                }
            ),
            maxlen=10000,
            approximate=True,
        )

        # 推送决策
        await self._push_decision(event, comp, sym)
        self._stats["events"] += 1

    # ── Consumer: cw4 signal.scored 反馈 ─────────────
    async def _consume_signal_scored(self):
        r = get_redis()
        while True:
            try:
                results = await r.xreadgroup(
                    CG_ONCHAIN,
                    "sentinel-scored",
                    {STREAM_SIGNAL_SCORED: ">"},
                    count=20,
                    block=1000,
                )
                if not results:
                    continue
                for _, messages in results:
                    for msg_id, fields in messages:
                        try:
                            d = decode(fields)
                            sym = d.get("symbol", "")
                            regime = d.get("regime", "UNKNOWN")
                            win_prob = float(d.get("win_probability", 0.5))
                            ci_lo = float(d.get("confidence_lo", 0.45))
                            ci_hi = float(d.get("confidence_hi", 0.55))
                            if sym in self._scorers:
                                self._regimes[sym] = regime
                                self._scorers[sym].update_from_scored(win_prob, ci_lo, ci_hi)
                            await r.xack(STREAM_SIGNAL_SCORED, CG_ONCHAIN, msg_id)
                        except Exception as e:
                            logger.error(f"handle_scored: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"consume_signal_scored: {e}")
                await asyncio.sleep(1)

    # ── 推送决策 ──────────────────────────────────────
    async def _push_decision(self, event: RawOnchainEvent, comp: dict, symbol: str):
        fused = comp.get("fused_win_prob")
        score = comp.get("onchain_score", 0)

        if fused is None:
            if abs(score) < 0.35 or event.severity not in ("critical", "high"):
                return
            level = "WATCH"
        elif fused >= PUSH_STRONG:
            level = "STRONG"
        elif fused >= PUSH_WATCH:
            level = "WATCH"
        else:
            return

        stats = get_signal_stats(symbol, event.classify(), window_h=24, lookback_days=90)
        msg = self._build_msg(event, comp, stats, level, symbol)

        # 写 system.alerts → cw4 NotificationHub 处理 Telegram 推送
        r = get_redis()
        await r.xadd(
            STREAM_SYSTEM_ALERTS,
            encode(
                {
                    "type": "onchain_signal",
                    "level": level,
                    "symbol": symbol,
                    "message": msg,
                    "fused_prob": str(fused or score),
                    "severity": event.severity,
                    "ts": str(time.time()),
                }
            ),
            maxlen=10000,
            approximate=True,
        )

        # 企微 / 飞书（cw4 NotificationHub 目前只有 Telegram + DingTalk）
        # 如配置了 WX/Feishu webhook，直接推
        if level == "STRONG":
            await self._push_wx(msg)
            await self._push_feishu(msg)

        self._stats["pushes"] += 1
        logger.info(f"PUSH [{level}] {symbol} fused={fused}")

    def _build_msg(self, event: RawOnchainEvent, comp: dict, stats: dict, level: str, symbol: str) -> str:
        icon = "🚨" if level == "STRONG" else "⚠️"
        fused = comp.get("fused_win_prob")
        cw4_p = comp.get("cw4_win_prob")
        score = comp.get("onchain_score", 0)
        flow = comp.get("net_exchange_flow", 0)
        price = self._prices.get(symbol, 0)
        regime = comp.get("regime", "UNKNOWN")

        lines = [
            f"{icon} *链上哨兵 [{level}]* | {symbol}",
            f"{'─' * 22}",
            f"📋 {event.classify().replace('_', ' ').title()}",
            f"💰 ${event.amount_usd / 1e6:.1f}M | {event.severity.upper()}",
            f"🌊 链上评分: {score:+.3f} | Regime: `{regime}`",
        ]
        if fused:
            lines += [
                f"🎯 融合胜率: *{fused:.0%}*  (cw4:{cw4_p:.0%} + 链上)",
                f"   CI [{comp.get('ci_lo', 0):.0%}, {comp.get('ci_hi', 0):.0%}]",
            ]
        if price:
            lines.append(f"💲 {symbol[:3]}: ${price:,.2f}")
        if flow != 0:
            lines.append(f"🏦 交易所{'净流出📈' if flow > 0 else '净流入📉'} {abs(flow):.1f} BTC")
        if stats.get("n", 0) >= 5:
            lines.append(f"📈 历史: {stats['summary']}")
        if level == "STRONG" and fused and fused >= PUSH_STRONG:
            action = "建议关注做多" if (fused or 0.5) >= 0.6 else "建议关注做空"
            lines += [f"{'─' * 22}", f"💡 *{action}*（仅供参考）"]
        lines.append(f"⏰ {datetime.utcnow().strftime('%H:%M:%S')} UTC")
        return "\n".join(lines)

    async def _push_wx(self, text: str):
        url = os.environ.get("WX_WEBHOOK", "")
        if not url:
            return
        try:
            await self._session.post(
                url, json={"msgtype": "markdown", "markdown": {"content": text}}, timeout=aiohttp.ClientTimeout(total=5)
            )
        except Exception as e:
            logger.warning(f"wx push: {e}")

    async def _push_feishu(self, text: str):
        url = os.environ.get("FEISHU_WEBHOOK", "")
        if not url:
            return
        try:
            await self._session.post(
                url, json={"msg_type": "text", "content": {"text": text}}, timeout=aiohttp.ClientTimeout(total=5)
            )
        except Exception as e:
            logger.warning(f"feishu push: {e}")

    # ── 价格轮询（CoinGecko，30s）────────────────────
    async def _price_updater(self):
        while True:
            try:
                async with self._session.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.ok:
                        d = await r.json()
                        self._prices["BTCUSDT"] = d.get("bitcoin", {}).get("usd", 0)
                        self._prices["ETHUSDT"] = d.get("ethereum", {}).get("usd", 0)
                        self._prices["SOLUSDT"] = d.get("solana", {}).get("usd", 0)
            except Exception as e:
                logger.warning(f"price_updater: {e}")
            await asyncio.sleep(30)

    # ── 时间衰减（每小时）────────────────────────────
    async def _decay_scheduler(self):
        r = get_redis()
        while True:
            await asyncio.sleep(3600)
            for scorer in self._scorers.values():
                scorer.decay(1.0)
                await r.set(f"onchain:state:{scorer.symbol}", json.dumps(scorer.get_state()), ex=3600)

    # ── SQLite 回填（每30分钟）───────────────────────
    async def _backfill_scheduler(self):
        async def price_fn(symbol, ts):
            return self._prices.get(symbol)

        while True:
            await asyncio.sleep(1800)
            await backfill_outcomes(price_fn)

    # ── HTTP API（挂在 gateway 聚合的 /api/v4/onchain）
    async def _http_server(self):
        app = web.Application()
        app.router.add_get("/health", self._h_health)
        app.router.add_get("/api/v4/onchain/state", self._h_state)
        app.router.add_get("/api/v4/onchain/stats/{sym}", self._h_stats)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("SERVICE_PORT", 8020))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info(f"HTTP: 0.0.0.0:{port}")

    async def _h_health(self, _):
        r = get_redis()
        ok = await r.ping()
        return web.json_response(
            {
                "status": "ok" if ok else "degraded",
                "service": "onchain-sentinel",
                "stats": self._stats,
                "ts": datetime.utcnow().isoformat(),
            }
        )

    async def _h_state(self, _):
        return web.json_response(
            {sym: scorer.get_state() for sym, scorer in self._scorers.items()}
            | {"prices": self._prices, "regimes": self._regimes}
        )

    async def _h_stats(self, req):
        sym = req.match_info["sym"].upper()
        cls = req.rel_url.query.get("class", "whale_inflow_exchange")
        window = int(req.rel_url.query.get("window", 24))
        result = get_signal_stats(sym, cls, window)
        wallet = req.rel_url.query.get("wallet")
        if wallet:
            result["wallet"] = get_wallet_stats(wallet, req.rel_url.query.get("chain", "ETH"))
        return web.json_response(result)


async def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s %(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    svc = OnchainService()
    await svc.start()


if __name__ == "__main__":
    asyncio.run(main())
