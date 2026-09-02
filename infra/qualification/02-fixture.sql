-- Deterministic PAPER fixture. No secrets, no live venues.
INSERT INTO signals (id, symbol, regime, signal_type, direction, win_probability, status)
VALUES (
    '00000000-0000-4000-8000-000000000001',
    'BTCUSDT',
    'RANGING',
    'LONG_SETUP',
    'LONG',
    0.60,
    'scored'
) ON CONFLICT (id) DO NOTHING;
