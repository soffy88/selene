"""market-scanner 纯函数 + signal 服务 backfill 契约测试。

扫描器是 v4 链缺失的前半截(market.candles / market.raw 生产者 + 小币趋势发现);
这里覆盖它的全部纯逻辑,以及 signal 服务对 backfill=true 的"只热身不评分"契约
(否则种子回放会对历史价格发信号并占用 1h 冷却窗)。
"""

import asyncio

import pytest

from services.scanner.main import (
    BinanceScanClient,  # noqa: F401  (import 健全性)
    compute_indicators,
    discover_candidates,
    ema_last,
    latest_closed,
    wilder_rsi,
)

# ── wilder_rsi ────────────────────────────────────────────────────────────────


def test_rsi_insufficient_history_is_none():
    assert wilder_rsi([100.0] * 14) is None  # 需要 period+1


def test_rsi_all_gains_is_100():
    closes = [100.0 + i for i in range(30)]
    assert wilder_rsi(closes) == 100.0


def test_rsi_all_losses_near_zero():
    closes = [100.0 - i for i in range(30)]
    assert wilder_rsi(closes) < 1.0


def test_rsi_flat_then_mixed_in_range():
    closes = [
        100,
        101,
        100,
        102,
        101,
        103,
        102,
        104,
        103,
        105,
        104,
        106,
        105,
        107,
        106,
        108,
        107,
        109,
        108,
        110,
    ]
    r = wilder_rsi([float(c) for c in closes])
    assert 50.0 < r < 100.0  # 净上涨 → 偏多但非极值


# ── ema_last ──────────────────────────────────────────────────────────────────


def test_ema_insufficient_is_none():
    assert ema_last([1.0] * 19, 20) is None


def test_ema_constant_series_is_constant():
    assert ema_last([5.0] * 60, 20) == pytest.approx(5.0)


def test_ema_tracks_recent_prices():
    closes = [100.0] * 50 + [200.0] * 50
    e20 = ema_last(closes, 20)
    e50 = ema_last(closes, 50)
    assert e20 > e50  # 短周期贴近新价格
    assert 100.0 < e50 < e20 <= 200.0


def test_compute_indicators_keys():
    ind = compute_indicators([float(i) for i in range(250)])
    assert set(ind) == {"rsi", "ema20", "ema50", "ema200"}
    assert all(v is not None for v in ind.values())


# ── discover_candidates ───────────────────────────────────────────────────────


def _tick(sym, change, qvol, last=1.0):
    return {
        "symbol": sym,
        "priceChangePercent": str(change),
        "quoteVolume": str(qvol),
        "lastPrice": str(last),
    }


def test_discovery_filters_and_ranks():
    tickers = [
        _tick("AAAUSDT", 25.0, 50e6),  # 强势
        _tick("BBBUSDT", 8.0, 30e6),  # 达标
        _tick("CCCUSDT", 40.0, 5e6),  # 量不够 → 排除
        _tick("DDDUSDT", 2.0, 100e6),  # 涨幅不够 → 排除
        _tick("BTCUSDT", 12.0, 9e9),  # 核心币 → 排除
        _tick("USDCUSDT", 6.0, 80e6),  # 稳定币对 → 排除
        _tick("EEEBUSD", 30.0, 90e6),  # 非 USDT 本位 → 排除
    ]
    out = discover_candidates(
        tickers,
        {},
        exclude={"BTCUSDT"},
        min_quote_volume=20e6,
        min_change_pct=5.0,
        top_n=5,
    )
    assert [c["symbol"] for c in out] == ["AAAUSDT", "BBBUSDT"]
    assert out[0]["score"] == pytest.approx(25.0)  # 无历史快照 surge=1.0


def test_discovery_volume_surge_boosts_rank():
    tickers = [_tick("AAAUSDT", 10.0, 60e6), _tick("BBBUSDT", 12.0, 30e6)]
    prev = {"AAAUSDT": 20e6, "BBBUSDT": 30e6}  # AAA 量能 3 倍激增
    out = discover_candidates(tickers, prev, exclude=set(), min_quote_volume=20e6, min_change_pct=5.0, top_n=2)
    assert out[0]["symbol"] == "AAAUSDT"  # 10×3.0=30 > 12×1.0
    assert out[0]["volume_surge"] == pytest.approx(3.0)  # 截断上限


def test_discovery_top_n_and_malformed_rows():
    tickers = [_tick(f"S{i}USDT", 5.0 + i, 30e6) for i in range(10)]
    tickers.append({"symbol": "BADUSDT"})  # 缺字段 → 跳过不炸
    out = discover_candidates(tickers, {}, exclude=set(), min_quote_volume=20e6, min_change_pct=5.0, top_n=3)
    assert len(out) == 3
    assert out[0]["symbol"] == "S9USDT"


# ── latest_closed ─────────────────────────────────────────────────────────────


def test_latest_closed_skips_forming_candle():
    h = 3_600_000
    ks = [{"open_time": 0}, {"open_time": h}, {"open_time": 2 * h}]
    # now = 2.5h:第三根(2h 开盘)还没收 → 取第二根
    assert latest_closed(ks, int(2.5 * h), h)["open_time"] == h
    # now = 3h 整:第三根刚收
    assert latest_closed(ks, 3 * h, h)["open_time"] == 2 * h


def test_latest_closed_none_when_all_forming():
    assert latest_closed([{"open_time": 100}], 200, 3_600_000) is None


# ── signal 服务 backfill 契约 ─────────────────────────────────────────────────


def _candle(backfill: bool) -> dict:
    d = {
        "symbol": "NEWUSDT",
        "high": 11.0,
        "low": 9.0,
        "close": 10.0,
        "indicators": {"rsi": 55.0, "ema20": 10.0, "ema50": 9.8, "ema200": 9.0},
    }
    if backfill:
        d["backfill"] = True
    return d


def test_backfill_candle_warms_state_but_never_scores(monkeypatch):
    from services.signal.main import SignalService

    svc = SignalService()
    calls = []

    async def _fake_score(*a, **k):
        calls.append(a)

    monkeypatch.setattr(svc, "_score_and_emit", _fake_score)
    asyncio.run(svc.handle_candle(_candle(backfill=True)))

    assert calls == []  # 不评分
    assert svc._market["NEWUSDT"]["price"] == 10.0  # 但缓存已热身
    assert svc._get_detector("NEWUSDT")._closes  # detector 吃到了 K 线


def test_live_candle_still_scores(monkeypatch):
    from services.signal.main import SignalService

    svc = SignalService()
    calls = []

    async def _fake_score(*a, **k):
        calls.append(a)

    async def _fake_hmm(symbol):
        return None

    monkeypatch.setattr(svc, "_score_and_emit", _fake_score)
    monkeypatch.setattr(svc, "_refresh_hmm", _fake_hmm)
    asyncio.run(svc.handle_candle(_candle(backfill=False)))

    assert len(calls) == 1
