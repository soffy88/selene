-- ============================================================
-- helixa_grants.sql
-- Grant read-only access from selene_app to helixa database.
--
-- Purpose: allow sel_v2 to read Helios market data (derivatives
--          snapshots, taker flow, OI, funding) without ETL copies.
--
-- Execution: kanpan (superuser) must run this script connected
--            to the helixa database:
--
--   psql -h localhost -p 5434 -U kanpan -d helixa -f helixa_grants.sql
--
-- This file creates NO objects in helixa. It only issues GRANT.
-- Nothing in selene is changed by this script.
-- ============================================================

-- Step 1: allow selene_app to open a connection to helixa
GRANT CONNECT ON DATABASE helixa TO selene_app;

-- Step 2: allow selene_app to see the public schema
GRANT USAGE ON SCHEMA public TO selene_app;

-- Step 3: table-level SELECT grants (read-only, no DML)

-- v2_derivatives_snapshots feed: OI + funding_rate + long_short_ratio
-- every ~112 s; symbols BTC/ETH/SOL on OKX.
-- Maps to: sel_v2 v2_derivatives_snapshots (oi, funding_rate, next_funding)
GRANT SELECT ON TABLE public.derivatives_snapshots TO selene_app;

-- 1-minute taker buy/sell volume windows for BTC/ETH/SOL SWAP.
-- Maps to: check_surging ofi_90pct STUB (proxy via net taker direction)
GRANT SELECT ON TABLE public.taker_flow_1m TO selene_app;

-- Historical funding rate records (sparse, used for backfill feasibility only)
GRANT SELECT ON TABLE public.funding_rate_history TO selene_app;

-- OI sampled every ~5 minutes (used for OI direction check in Strategy 1 Step 4b)
GRANT SELECT ON TABLE public.open_interest_history TO selene_app;

-- LOB orderbook snapshots — currently 0 rows but collector may start.
-- Granted now so no schema change is needed when data arrives.
GRANT SELECT ON TABLE public.orderbook TO selene_app;

-- OHLCV bars (all timeframes, all symbols).
-- selene.v2_bars_4h already has deeper history (2024-01-01 vs 2024-11-05),
-- but granting allows future use (e.g. 1m bars as tick proxy if needed).
GRANT SELECT ON TABLE public.ohlcv TO selene_app;

-- ============================================================
-- Revocation (run if access needs to be withdrawn):
-- ============================================================
--
-- REVOKE SELECT ON TABLE public.derivatives_snapshots FROM selene_app;
-- REVOKE SELECT ON TABLE public.taker_flow_1m           FROM selene_app;
-- REVOKE SELECT ON TABLE public.funding_rate_history    FROM selene_app;
-- REVOKE SELECT ON TABLE public.open_interest_history   FROM selene_app;
-- REVOKE SELECT ON TABLE public.orderbook               FROM selene_app;
-- REVOKE SELECT ON TABLE public.ohlcv                   FROM selene_app;
-- REVOKE USAGE  ON SCHEMA public                        FROM selene_app;
-- REVOKE CONNECT ON DATABASE helixa                     FROM selene_app;
--
-- ============================================================
