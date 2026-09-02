"""StateEventEmitter: publishes state change events to Redis Stream (Wave 4)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .schema import StateChangeEvent, StateOutput

logger = logging.getLogger(__name__)

_CURRENT_STATE_KEY_PREFIX = "sel:current_state"


class StateEventEmitter:
    """Publishes state changes to a Redis Stream and maintains current-state hash."""

    STREAM_KEY_PREFIX = "sel:state_changes"
    MAX_STREAM_LEN = 10000

    async def emit_if_changed(
        self,
        redis,
        symbol: str,
        prev_state: Optional[str],
        current: StateOutput,
    ) -> bool:
        """
        Publish to Redis Stream only if state changed from prev_state.
        Also writes current state to Redis hash for direct lookup.
        Returns True if a stream event was emitted.
        """
        # Always update the current-state hash so paper trading can read latest
        await self._write_current_state(redis, symbol, current)

        if prev_state == current.state:
            return False

        event = StateChangeEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol=symbol,
            previous_state=prev_state,
            new_state=current.state,
            bar_time=current.bar_time,
            reason=current.reason,
        )

        stream_key = f"{self.STREAM_KEY_PREFIX}:{symbol}"
        fields = {
            "event_time": event.event_time.isoformat(),
            "symbol": event.symbol,
            "previous_state": event.previous_state or "",
            "new_state": event.new_state or "",
            "bar_time": event.bar_time.isoformat(),
            "reason": event.reason,
        }

        await redis.xadd(
            stream_key,
            fields,
            maxlen=self.MAX_STREAM_LEN,
            approximate=True,
        )
        logger.info(
            "state_change emitted: %s %s -> %s at %s",
            symbol,
            prev_state,
            current.state,
            current.bar_time,
        )
        return True

    async def _write_current_state(self, redis, symbol: str, output: StateOutput) -> None:
        """Write current state as JSON to Redis hash for direct lookup."""
        key = f"{_CURRENT_STATE_KEY_PREFIX}:{symbol}"
        payload = {
            "bar_time": output.bar_time.isoformat(),
            "symbol": output.symbol,
            "state": output.state,
            "direction": output.direction,
            "is_cold_start": output.is_cold_start,
            "confidence": output.confidence,
            "reason": output.reason,
            "is_legal_transition": output.is_legal_transition,
            "transition_from": output.transition_from,
            "health_warning": output.health_warning,
            "close": output.close,
            "sigma_p_24h": output.sigma_p_24h,
            "H": output.H,
            "TF": output.TF,
            "OI": output.OI,
            "funding_rate": output.funding_rate,
            "LV": output.LV,
            "feature_completeness": output.feature_completeness,
        }
        await redis.set(key, json.dumps(payload))
