"""Engine-vs-harness reconciliation for the S2 1s channel (Wave S2G Part 5 gate).

The offline equivalence proven in S2G-0 does not carry over to the wiring: the
harness walks the 1s series in ONE continuous pass, while the engine walks it in
chunks bounded by 4H bar closes (`_advance_1s_channel` is called once per bar with
a `prev_bar_unix` cursor). Chunking is the one new variable, and this script is
what proves it changes nothing.

Both paths share the accumulator, the event layer and the standardisation, so a
non-zero diff means the CHUNKING is wrong — a dropped or double-counted second at
a bar boundary — not that the maths drifted.

Runs entirely offline against a frozen window. It never touches the running
paper-engine container, never writes a live table, and never places an order.

Run:  REPLAY_END='2026-07-19 08:00:00+00:00' python -m sel_v2.offline.s2g_engine_reconcile
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import numpy as np

from sel_v2.data.tick_1s import zscores_1s
from sel_v2.strategies.cusum_short import CUSUMShort
from sel_v2.strategies.s2_event_layer import S2EventLayer

logger = logging.getLogger(__name__)

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
REPLAY_END = (
    datetime.fromisoformat(os.environ["REPLAY_END"])
    if os.environ.get("REPLAY_END")
    else None
)
BAR_SECONDS = 4 * 3600  # the cadence the engine chunks by
EXPORT = os.environ.get("S2G_EXPORT", "/tmp/s2g_1s.npz")


def load_1s_from_export(path: str):
    """Read the frozen window from the export produced by s2g_export_1s.

    Deliberately file-based: the comparison below walks 1.1M seconds twice, and
    doing that while holding a database cursor is what stalled v2_ticks
    persistence for 3h20m on 2026-07-19. The export is chunked and time-limited;
    everything after it is offline, so that failure mode is structurally gone
    rather than merely avoided.
    """
    d = np.load(path)
    secs, px = d["secs"], d["px"]
    full = np.arange(secs[0], secs[-1] + 1.0, 1.0)
    idx = np.searchsorted(secs, full, side="right") - 1
    return full, zscores_1s(px[idx])


def harness_events(secs, zs) -> list:
    """Reference path: one continuous pass, as cusum_1s_replay walks it."""
    acc, layer, out = CUSUMShort(), S2EventLayer(), []
    for k in range(len(secs)):
        trig = acc.update(float(zs[k]), float(secs[k]))
        if not trig.triggered:
            continue
        ev = layer.on_trigger(
            datetime.fromtimestamp(float(secs[k]), timezone.utc),
            trig.direction,
            max(trig.cusum_positive, trig.cusum_negative),
        )
        if ev is not None:
            out.append(ev)
    return out


def engine_events(secs, zs) -> list:
    """Engine path: the real `_advance_1s_channel`, driven in 4H bar chunks."""
    from sel_v2.paper.strategy_engine import PaperStrategyEngine

    eng = PaperStrategyEngine(
        s2_event_layer=S2EventLayer(), skip_hawkes=True, skip_tda=True
    )
    out, prev = [], None
    # bar closes covering the window, mirroring how process_frame advances
    start, end = float(secs[0]), float(secs[-1])
    edges = list(np.arange(start + BAR_SECONDS, end + BAR_SECONDS, BAR_SECONDS))
    if not edges or edges[-1] < end:
        edges.append(end)
    for bar_unix in edges:
        out.extend(eng._advance_1s_channel((secs, zs), prev, float(bar_unix)))
        prev = float(bar_unix)
    return out


def main() -> int:
    secs, zs = load_1s_from_export(EXPORT)
    logger.info("frozen window from %s: %d seconds", EXPORT, len(secs))
    ref = harness_events(secs, zs)
    eng = engine_events(secs, zs)

    ref_key = {(e.eval_ts, e.direction, e.excursion_count) for e in ref}
    eng_key = {(e.eval_ts, e.direction, e.excursion_count) for e in eng}
    only_ref = ref_key - eng_key
    only_eng = eng_key - ref_key

    logger.info(
        "harness events=%d  engine events=%d  only_harness=%d  only_engine=%d",
        len(ref),
        len(eng),
        len(only_ref),
        len(only_eng),
    )
    for k in sorted(only_ref)[:5]:
        logger.warning("only in harness: %s", k)
    for k in sorted(only_eng)[:5]:
        logger.warning("only in engine: %s", k)

    if only_ref or only_eng:
        logger.error("RECONCILE FAIL — diff != 0; the bar chunking is not transparent")
        return 1
    logger.info("RECONCILE OK — diff = 0")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(main())
