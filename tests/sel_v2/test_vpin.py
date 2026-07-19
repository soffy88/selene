"""Tests for sel_v2.observation_tools.vpin (ICT-1 streaming VPIN calculator)."""

from datetime import datetime, timedelta, timezone

import pytest

# sel_v2.observation_tools pulls in the private quant stack (oprim). Skip the
# module outright when it is absent so CI reports an explicit skip instead of a
# collection error that aborts the whole run.
pytest.importorskip("oprim", reason="private quant stack (oprim) not installed")

from sel_v2.observation_tools.vpin import VPINCalculator

T0 = datetime(2026, 7, 6, tzinfo=timezone.utc)


def _ts(minutes):
    return T0 + timedelta(minutes=minutes)


def test_bucket_boundary_trade_splits_pro_rata():
    calc = VPINCalculator(v_bucket=10.0, n_buckets=2, warmup_buckets=2)
    calc.on_tick(_ts(0), 100.0, 6.0, "buy")
    # 7-unit sell: 4 close bucket 1, 3 spill into bucket 2
    done = calc.on_tick(_ts(1), 101.0, 7.0, "sell")
    assert len(done) == 1
    b = done[0]
    assert b.v_total == pytest.approx(10.0)
    assert b.v_buy == pytest.approx(6.0)
    assert b.v_sell == pytest.approx(4.0)
    # a single giant trade can complete multiple buckets
    done = calc.on_tick(_ts(2), 102.0, 27.0, "buy")
    assert len(done) == 3  # 3 remaining + 27 = 30 → buckets of 10 each
    assert all(x.v_total == pytest.approx(10.0) for x in done)


def test_vpin_hand_computed_over_window():
    calc = VPINCalculator(v_bucket=10.0, n_buckets=2, warmup_buckets=2)
    assert calc.vpin is None  # window not filled
    calc.on_tick(_ts(0), 100.0, 8.0, "buy")
    calc.on_tick(_ts(1), 100.5, 2.0, "sell")  # bucket 1: |8-2|/10 = 0.6
    assert calc.vpin is None
    calc.on_tick(_ts(2), 101.0, 5.0, "buy")
    calc.on_tick(_ts(3), 101.5, 5.0, "sell")  # bucket 2: |5-5|/10 = 0.0
    assert calc.vpin == pytest.approx((6.0 + 0.0) / 20.0)
    calc.on_tick(_ts(4), 102.0, 10.0, "buy")  # bucket 3: |10-0| = 10
    assert calc.vpin == pytest.approx((0.0 + 10.0) / 20.0)  # rolling window of 2


def test_bvc_direction_agrees_with_price_moves():
    calc = VPINCalculator(v_bucket=1.0, n_buckets=5, warmup_buckets=5)
    price = 100.0
    # alternate small moves to build the z-window, then a strong up-bucket
    for i in range(20):
        price *= 1.0005 if i % 2 else 0.9995
        calc.on_tick(_ts(i), price, 1.0, "buy" if i % 2 else "sell")
    up = calc.on_tick(_ts(30), price * 1.02, 1.0, "buy")
    assert len(up) == 1 and up[0].bvc_buy_frac is not None
    assert up[0].bvc_buy_frac > 0.9  # strong up move → BVC calls it buy-heavy
    down = calc.on_tick(_ts(31), price * 0.98, 1.0, "sell")
    assert down[0].bvc_buy_frac < 0.1


def test_percentile_none_before_warmup_then_available():
    calc = VPINCalculator(v_bucket=2.0, n_buckets=3, warmup_buckets=10)
    for i in range(9 + 3):
        calc.on_tick(_ts(i), 100.0 + i * 0.1, 2.0, "buy" if i % 3 else "sell")
        if calc.completed_buckets < 10:
            assert calc.percentile(95) is None
    assert calc.completed_buckets >= 10
    p50, p95 = calc.percentile(50), calc.percentile(95)
    assert p50 is not None and p95 is not None and p50 <= p95


def test_replay_determinism():
    def run():
        calc = VPINCalculator(v_bucket=3.0, n_buckets=4, warmup_buckets=4)
        readings = []
        price = 100.0
        for i in range(200):
            price *= 1.001 if (i * 7) % 3 else 0.999
            side = "buy" if (i * 5) % 2 else "sell"
            calc.on_tick(_ts(i), price, 1.0 + (i % 4), side)
            readings.append((calc.vpin, calc.bvc_vpin))
        return readings

    assert run() == run()


def test_rejects_nonpositive_bucket():
    with pytest.raises(ValueError):
        VPINCalculator(v_bucket=0.0)
