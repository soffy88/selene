"""
CryptoWatch v4 — Notification Hub
Consumes system.alerts + order.lifecycle streams and fans out to channels.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import aiohttp

from shared.events.streams import (
    STREAM_SYSTEM_ALERTS,
    STREAM_ORDER_LIFECYCLE,
    CG_NOTIFY,
    consume,
    StreamEvent,
)

logger = logging.getLogger(__name__)


class TelegramChannel:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{token}"
        self._enabled = bool(token and chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        # api.telegram.org 从容器只经 helios-proxy 可达;直连(此前的实现)静默超时,
        # V4 的 telegram 通知一直未真正送达。默认走 2080,可用 TELEGRAM_PROXY 覆盖。
        self._proxy = os.getenv("TELEGRAM_PROXY", "http://helios-proxy:2080") or None

    async def _sess(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, text: str, reply_markup: dict = None) -> bool:
        if not self._enabled:
            return False
        s = await self._sess()
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with s.post(
                f"{self._base}/sendMessage",
                json=payload,
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                return r.status == 200
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    async def send_signal(
        self, signal: dict, order_id: str = None, exec_mode: str = "NOTIFY_ONLY"
    ) -> bool:
        """Format and send a scored signal notification."""
        sig_type = signal.get("signal_type", "SIGNAL")
        symbol = signal.get("symbol", "")
        prob = signal.get("win_probability", 0)
        price = signal.get("entry_price", signal.get("price", 0))
        regime = signal.get("regime", "")
        action = signal.get("action", "")
        ind = signal.get("indicators", {})

        lines = [
            f"⚡ *{sig_type}* | {symbol}",
            f"{'─' * 22}",
            f"💲 价格: ${price:,.4f}",
            f"🎯 胜率: {prob:.0%} | Regime: `{regime}`",
        ]
        if ind.get("funding_rate") is not None:
            lines.append(f"📊 资金费率: {ind['funding_rate']:+.4f}%")
        if ind.get("rsi") is not None:
            lines.append(f"📉 RSI: {ind['rsi']:.1f}")
        if action:
            lines.append(f"💡 {action}")

        if exec_mode == "CONFIRM_THEN_EXEC" and order_id:
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ 确认", "callback_data": f"confirm:{order_id}"},
                        {"text": "❌ 拒绝", "callback_data": f"reject:{order_id}"},
                    ]
                ]
            }
            return await self.send("\n".join(lines), keyboard)
        return await self.send("\n".join(lines))

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class DingTalkChannel:
    def __init__(self, webhook_url: str):
        self._url = webhook_url
        self._enabled = bool(webhook_url)

    async def send(self, title: str, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    self._url,
                    json={
                        "msgtype": "markdown",
                        "markdown": {"title": title, "text": text},
                    },
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    return r.status == 200
        except Exception as e:
            logger.error(f"DingTalk error: {e}")
            return False


class NotificationHub:
    """
    Consumes Redis Stream events and fans out to notification channels.
    Designed to run as an independent microservice.
    """

    def __init__(
        self, telegram: TelegramChannel = None, dingtalk: DingTalkChannel = None
    ):
        self._tg = telegram
        self._dd = dingtalk
        self._stats = {"sent": 0, "errors": 0}

    async def _on_alert(self, event: StreamEvent):
        data = event.data
        alert_t = data.get("type", "")
        msg = data.get("message", str(data))

        if alert_t == "circuit_breaker":
            text = (
                f"🔴 *熔断器触发*\n原因: {data.get('reason', '')}\n"
                f"今日PnL: {data.get('daily_pnl', 0):+.2f}\n⛔ 所有交易已暂停"
            )
        elif alert_t == "signal":
            if self._tg:
                await self._tg.send_signal(
                    data, exec_mode=data.get("exec_mode", "NOTIFY_ONLY")
                )
            self._stats["sent"] += 1
            return
        elif alert_t == "onchain_signal":
            # onchain-sentinel 写入的链上预警，直接用 message 字段
            level = data.get("level", "WATCH")
            text = data.get("message", msg)
            # STRONG 信号额外加紧急标记
            if level == "STRONG":
                tasks = []
                if self._tg:
                    tasks.append(self._tg.send(text))
                if self._dd:
                    tasks.append(self._dd.send("链上哨兵 STRONG", text))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                self._stats["sent"] += sum(1 for r in results if r is True)
            else:
                if self._tg:
                    await self._tg.send(text)
                    self._stats["sent"] += 1
            return
        elif alert_t == "risk_alert":
            text = f"⚠️ *风控告警*\n{data.get('reason', '')}"
        else:
            text = f"ℹ️ *系统通知*\n{msg}"

        tasks = []
        if self._tg:
            tasks.append(self._tg.send(text))
        if self._dd:
            tasks.append(self._dd.send("CryptoWatch v4", text))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        self._stats["sent"] += ok

    async def _on_order(self, event: StreamEvent):
        data = event.data
        ev = data.get("event", "")
        state = data.get("state", "")
        # 开仓:PAPER 撮合发 "filled_immediately"(此时 state 已是 MONITORING),
        # live 交易所回报发 "filled"——两者都要通知(此前只看 state==FILLED,PAPER
        # 开仓永远漏发)。平仓/失败仍按状态判断。
        is_open = ev in ("filled", "filled_immediately")
        is_closed = ev == "closed" or state == "CLOSED"
        is_failed = state == "FAILED"
        if not (is_open or is_closed or is_failed):
            return

        symbol = data.get("symbol", "")
        side = data.get("side", "")
        pnl = data.get("realized_pnl")

        if is_open:
            text = (
                f"✅ *V4 开仓*\n{symbol} {side}\n"
                f"价格: ${data.get('filled_price', 0):,.6f}\n"
                f"数量: {data.get('filled_qty', 0)}\n"
                f"止损: ${data.get('stop_loss', 0):,.6f}\n"
                f"止盈: ${data.get('take_profit', 0):,.6f}"
            )
        elif is_closed:
            pnl_str = f"${pnl:+.2f}" if pnl is not None else "N/A"
            text = f"📊 *V4 平仓*\n{symbol}\nPnL: {pnl_str}\n原因: {data.get('close_reason', '')}"
        else:
            text = f"❌ *V4 订单失败*\n{symbol}\n原因: {data.get('reject_reason', '')}"

        if self._tg:
            await self._tg.send(text)
        self._stats["sent"] += 1

    async def run(self):
        """Main loop — consume streams forever."""
        tasks = [
            asyncio.create_task(
                consume(
                    STREAM_SYSTEM_ALERTS, CG_NOTIFY, "notify-alerts", self._on_alert
                )
            ),
            asyncio.create_task(
                consume(
                    STREAM_ORDER_LIFECYCLE, CG_NOTIFY, "notify-orders", self._on_order
                )
            ),
        ]
        logger.info("NotificationHub: started")
        await asyncio.gather(*tasks)

    def get_stats(self) -> dict:
        return self._stats


async def main():
    import os
    from shared.db.redis_client import init_redis

    init_redis(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    tg = TelegramChannel(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    dd = DingTalkChannel(os.environ.get("DINGTALK_WEBHOOK_URL", ""))

    hub = NotificationHub(tg, dd)
    await hub.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
