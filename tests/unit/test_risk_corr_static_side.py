"""Static correlation-gate side-normalisation tests (audit P1-5).

Stored positions carry side "LONG"/"SHORT"; the incoming order side is "BUY"/"SELL".
The static fallback compared them raw (== ), so they never matched and the gate silently
passed all correlated same-direction concentration at cold start (exactly when the dynamic
realized-correlation path has no data and the conservative fallback is most needed).
"""
import services.risk.main as rm


def _set(monkeypatch, positions, equity=10_000.0):
    monkeypatch.setattr(rm, "_open_positions",
                        {"__equity__": {"value": equity}, **positions}, raising=False)


def test_static_fallback_blocks_correlated_same_direction(monkeypatch):
    # Two longs in the L1 group already at 35% of equity (stored as LONG)...
    _set(monkeypatch, {
        "BTCUSDT": {"side": "LONG", "notional": 2_000.0},
        "ETHUSDT": {"side": "LONG", "notional": 1_500.0},
    })
    # ...a new BUY (=LONG) in the same group must now be seen as same-direction and rejected.
    ok, reason = rm._gate.check_corr_exposure("SOLUSDT", "BUY")
    assert ok is False
    assert "corr_exposure" in reason


def test_static_fallback_allows_opposite_direction(monkeypatch):
    # Existing exposure is SHORT; a new BUY is the opposite direction → not concentrated.
    _set(monkeypatch, {
        "BTCUSDT": {"side": "SHORT", "notional": 5_000.0},
        "ETHUSDT": {"side": "SHORT", "notional": 5_000.0},
    })
    ok, _ = rm._gate.check_corr_exposure("SOLUSDT", "BUY")
    assert ok is True


def test_static_fallback_ignores_other_groups(monkeypatch):
    _set(monkeypatch, {"DOGEUSDT": {"side": "LONG", "notional": 9_000.0}})
    # SOL is in the L1 group, DOGE is in Meme → no shared-group concentration.
    ok, _ = rm._gate.check_corr_exposure("SOLUSDT", "BUY")
    assert ok is True
