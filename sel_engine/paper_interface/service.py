"""StateOutputService: main integration point for Wave 4 paper trading interface."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sel_engine.features.calculator import FeatureCalculator
from sel_engine.features.schema import FeatureVector
from sel_engine.states.engine import StateEngine
from sel_engine.states.schema import StateLabel, StateRecord

from .events import StateEventEmitter
from .schema import StateOutput
from .store import StateStore

logger = logging.getLogger(__name__)


class StateOutputService:
    """
    Called every 1H bar. Computes features → state → persists → emits events.
    Designed to be called from data-service's hourly candle hook or a standalone scheduler.
    """

    def __init__(self, symbol: str = "BTCUSDT") -> None:
        self.symbol = symbol
        self.engine = StateEngine(symbol)
        self.calculator = FeatureCalculator()
        self.store = StateStore()
        self.emitter = StateEventEmitter()
        self._prev_state: Optional[str] = None
        self._close_history: list[float] = []
        self._sigma_history: list[float] = []
        self._H_history: list[float] = []
        self._OI_history: list[float] = []

    async def process_bar(
        self,
        bar_time: datetime,
        close: float,
        pg_pool,
        redis,
        oi_current: Optional[float] = None,
        H_samples: Optional[list[float]] = None,
        bids: Optional[list] = None,
        asks: Optional[list] = None,
    ) -> StateOutput:
        """Process one 1H bar end: compute features → recognize state → persist → emit."""
        self._close_history.append(close)

        # Keep history bounded (max 800 bars for rolling windows)
        if len(self._close_history) > 800:
            self._close_history = self._close_history[-800:]

        fv = await self.calculator.compute(
            bar_time=bar_time,
            symbol=self.symbol,
            closes=self._close_history,
            sigma_p_history=self._sigma_history,
            H_history=self._H_history,
            OI_history=self._OI_history,
            OI_current=oi_current,
            total_depth_24h_mean=None,   # TODO: compute from orderbook history
            spread_bps_24h_mean=None,
            bids=bids,
            asks=asks,
            H_samples=H_samples,
            redis=redis,
            bar_ts_iso=bar_time.isoformat(),
        )

        # Update derived histories
        if fv.sigma_p_24h is not None:
            self._sigma_history.append(fv.sigma_p_24h)
            if len(self._sigma_history) > 100:
                self._sigma_history = self._sigma_history[-100:]
        if fv.H is not None:
            self._H_history.append(fv.H)
            if len(self._H_history) > 100:
                self._H_history = self._H_history[-100:]
        if fv.OI is not None:
            self._OI_history.append(fv.OI)
            if len(self._OI_history) > 100:
                self._OI_history = self._OI_history[-100:]

        record = self.engine.process(fv)
        output = self._to_output(record, fv)

        await self.store.upsert(pg_pool, output)
        await self.emitter.emit_if_changed(redis, self.symbol, self._prev_state, output)
        self._prev_state = output.state

        return output

    def _to_output(self, record: StateRecord, fv: FeatureVector) -> StateOutput:
        """Convert a StateRecord + FeatureVector into a StateOutput."""
        # Extract direction from state label
        direction: Optional[str] = None
        if record.state == StateLabel.SURGING_UP:
            direction = "Up"
        elif record.state == StateLabel.SURGING_DOWN:
            direction = "Down"

        # Feature completeness: fraction of named features that are non-None
        all_features = [
            fv.close,
            fv.sigma_p_24h,
            fv.H,
            fv.TF,
            fv.OI,
            fv.funding_rate,
            fv.LV,
            fv.delta_p_pct,
            fv.price_autocorr_12h,
            fv.price_autocorr_24h,
            fv.OI_hurst,
        ]
        completeness = sum(1 for f in all_features if f is not None) / len(all_features)

        # Health warning from HealthMonitor's last report
        report = self.engine.health.generate()
        health_warning = bool(report.health_warnings)

        return StateOutput(
            bar_time=record.time,
            symbol=record.symbol,
            state=record.state.value if record.state else None,
            state_enum=record.state,
            direction=direction,
            is_cold_start=record.cold_start,
            confidence=1.0,
            reason=record.reason,
            is_legal_transition=record.is_legal_transition,
            transition_from=record.transition_from.value if record.transition_from else None,
            health_warning=health_warning,
            close=fv.close if fv.close != 0.0 else None,
            sigma_p_24h=fv.sigma_p_24h,
            H=fv.H,
            TF=fv.TF,
            OI=fv.OI,
            funding_rate=fv.funding_rate,
            LV=fv.LV,
            feature_completeness=completeness,
        )
