"""NotificationHub tests — no network (optimization item #23).

The notification service previously had zero tests. These exercise construction,
stats, and that alert/order fan-out with no channels configured is a safe no-op.
"""

import asyncio

from services.notification.hub import NotificationHub
from shared.events.streams import StreamEvent


def _event(data):
    return StreamEvent(stream="system.alerts", msg_id="1-0", data=data)


def test_construct_and_stats():
    hub = NotificationHub()  # no channels
    assert hub.get_stats() == {"sent": 0, "errors": 0}


def test_on_alert_no_channels_is_noop():
    hub = NotificationHub()
    # circuit_breaker alert with no channels must not raise or count a send.
    asyncio.run(hub._on_alert(_event({"type": "circuit_breaker", "reason": "x", "daily_pnl": -1.0})))
    assert hub.get_stats()["sent"] == 0


def test_on_alert_generic_message_no_channels():
    hub = NotificationHub()
    asyncio.run(hub._on_alert(_event({"type": "other", "message": "hi"})))
    assert hub.get_stats()["errors"] == 0


class _CaptureTG:
    """Telegram stand-in that records sent text instead of hitting the network."""

    def __init__(self):
        self.sent = []

    async def send(self, text, reply_markup=None):
        self.sent.append(text)
        return True


def _order_event(data):
    return StreamEvent(stream="order.lifecycle", msg_id="1-0", data=data)


def test_on_order_notifies_on_paper_open():
    """PAPER open publishes event=filled_immediately with state already MONITORING —
    must still notify (regression: old code only matched state==FILLED)."""
    hub = NotificationHub()
    tg = _CaptureTG()
    hub._tg = tg
    asyncio.run(
        hub._on_order(
            _order_event(
                {
                    "event": "filled_immediately",
                    "state": "MONITORING",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "filled_price": 64000,
                }
            )
        )
    )
    assert len(tg.sent) == 1 and "开仓" in tg.sent[0] and "BTCUSDT" in tg.sent[0]


def test_on_order_notifies_on_live_open():
    hub = NotificationHub()
    tg = _CaptureTG()
    hub._tg = tg
    asyncio.run(
        hub._on_order(
            _order_event(
                {
                    "event": "filled",
                    "state": "FILLED",
                    "symbol": "ETHUSDT",
                    "side": "BUY",
                }
            )
        )
    )
    assert len(tg.sent) == 1 and "开仓" in tg.sent[0]


def test_on_order_notifies_on_close():
    hub = NotificationHub()
    tg = _CaptureTG()
    hub._tg = tg
    asyncio.run(
        hub._on_order(
            _order_event(
                {
                    "event": "closed",
                    "state": "CLOSED",
                    "symbol": "SOLUSDT",
                    "realized_pnl": 12.5,
                }
            )
        )
    )
    assert len(tg.sent) == 1 and "平仓" in tg.sent[0]


def test_on_order_ignores_intermediate_states():
    hub = NotificationHub()
    tg = _CaptureTG()
    hub._tg = tg
    asyncio.run(hub._on_order(_order_event({"event": "submitted", "state": "SUBMITTING", "symbol": "BTCUSDT"})))
    assert len(tg.sent) == 0
