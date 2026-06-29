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
