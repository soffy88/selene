"""
market-scanner — v4 信号链的缺失前半截:小币种趋势发现 + 行情喂送。

v4 链(signal → portfolio → risk → execution)架构完整但一直"无头":
`market.candles` / `market.raw` 两个输入流全仓库零生产者,signal.scored 恒为 0。
本服务补上这两个生产者,并实现 v4 的设计意图——发现有大涨趋势的小币种并跟进:

  1. 发现循环(SCAN_INTERVAL_S):一次 REST 调用拉全市场 24h ticker(Binance USD-M,
     与 execution adapter 同一 venue),按【流动性下限 × 24h 涨幅 × 量能激增】过滤排序,
     取 top-N 作为跟踪对象;结果写 `cw4:moonshots`(gateway /api/v4/moonshots 因此复活)。
  2. 喂送循环(FEED_INTERVAL_S):对 watchlist(常驻核心币 + 发现的小币)——
     - 新符号先回填 SEED_BARS 根 1h K 线(带 backfill=true 标记:signal 服务只用它们
       热身 regime 检测器,不评分——否则会对历史价格发信号并占用冷却窗);
     - 之后每根新收盘 K 线发一条 `market.candles`(指标 RSI/EMA20/50/200 由本服务算好);
     - 周期性发 `market.raw`(funding/OI 变化/多空比),触发 signal 服务即时重评分。

  评分/仓位/风控/执行整条下游链是 symbol 无关的,零改动直接工作。
  EXEC_MODE 保持 NOTIFY_ONLY(STATUS.md 🔒 Never)——本服务只让信号流动起来。

指标单位契约(services/signal/factors/composite.py):rsi 0-100;ema* 为价格;
funding_history 为原始费率列表(≥20 满分);oi_change_pct / price_change_pct 为百分数;
long_ratio 为 0-100 的多头账户占比。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from shared.db.redis_client import get_redis, publish
from shared.events.streams import STREAM_MARKET_CANDLES, STREAM_MARKET_RAW

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("market-scanner")

BINANCE_FAPI = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com")
PROXY = os.environ.get("SCANNER_PROXY") or os.environ.get("HTTPS_PROXY") or None

SCAN_INTERVAL_S = int(os.environ.get("SCAN_INTERVAL_S", "300"))
FEED_INTERVAL_S = int(os.environ.get("FEED_INTERVAL_S", "60"))
RAW_REFRESH_S = int(os.environ.get("RAW_REFRESH_S", "180"))
CANDLE_INTERVAL = os.environ.get("CANDLE_INTERVAL", "1h")
SEED_BARS = int(os.environ.get("SEED_BARS", "250"))

MIN_QUOTE_VOLUME_USD = float(os.environ.get("MIN_QUOTE_VOLUME_USD", "20000000"))
MIN_24H_CHANGE_PCT = float(os.environ.get("MIN_24H_CHANGE_PCT", "5.0"))
TOP_N = int(os.environ.get("SCANNER_TOP_N", "5"))

# 常驻核心币:HMM regime 检测器覆盖的三个 + 始终有信号面
CORE_SYMBOLS = [
    s.strip()
    for s in os.environ.get("CORE_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
    if s.strip()
]
# 发现阶段排除:核心币(单列)+ 稳定币/法币对(涨跌无趋势意义)
_STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "EUR", "USDP", "AEUR", "USD1"}

_INTERVAL_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


# ── 纯函数(可单测)────────────────────────────────────────────────────────────


def wilder_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder RSI(0-100)。历史不足返回 None(下游按缺数据处理,不伪造 50)。"""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 4)


def ema_last(closes: list[float], period: int) -> Optional[float]:
    """EMA 末值(SMA 起步 + 标准递推)。历史不足返回 None。"""
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2.0 / (period + 1)
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def compute_indicators(closes: list[float]) -> dict:
    return {
        "rsi": wilder_rsi(closes),
        "ema20": ema_last(closes, 20),
        "ema50": ema_last(closes, 50),
        "ema200": ema_last(closes, 200),
    }


def discover_candidates(
    tickers: list[dict],
    prev_volumes: dict[str, float],
    *,
    exclude: set[str],
    min_quote_volume: float = MIN_QUOTE_VOLUME_USD,
    min_change_pct: float = MIN_24H_CHANGE_PCT,
    top_n: int = TOP_N,
) -> list[dict]:
    """从 24h ticker 全量里筛出趋势小币候选,按 涨幅×量能激增 打分取 top-N。

    - 只看 USDT 本位、排除核心币和稳定币对(它们的"涨幅"没有跟仓意义);
    - quoteVolume 下限是流动性/可执行性底线(execution 的滑点检查也依赖流动性);
    - 量能激增 = 当前 24h quoteVolume / 上一轮扫描值,截断到 [0.5, 3.0]
      (没有上一轮快照时取 1.0,纯靠涨幅排序)。
    """
    out = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in exclude:
            continue
        if sym[:-4] in _STABLE_BASES:
            continue
        try:
            change = float(t["priceChangePercent"])
            qvol = float(t["quoteVolume"])
            last = float(t["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue
        if qvol < min_quote_volume or change < min_change_pct or last <= 0:
            continue
        prev = prev_volumes.get(sym)
        surge = max(0.5, min(3.0, qvol / prev)) if prev and prev > 0 else 1.0
        out.append(
            {
                "symbol": sym,
                "change_pct": change,
                "quote_volume": qvol,
                "volume_surge": round(surge, 3),
                "last_price": last,
                "score": round(change * surge, 4),
            }
        )
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:top_n]


def latest_closed(klines: list[dict], now_ms: int, interval_ms: int) -> Optional[dict]:
    """K 线列表(升序)里最新的【已收盘】一根:open_time + interval <= now。"""
    for k in reversed(klines):
        if k["open_time"] + interval_ms <= now_ms:
            return k
    return None


# ── Binance USD-M REST(经 helios-proxy)──────────────────────────────────────


class BinanceScanClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base: str = BINANCE_FAPI,
        proxy: Optional[str] = PROXY,
    ) -> None:
        self._s = session
        self._base = base.rstrip("/")
        self._proxy = proxy

    async def _get(self, path: str, params: Optional[dict] = None):
        """GET + 3 次重试(指数退避)。helios-proxy 上游 socks5 阵发性拒绝连接
        (实测 2026-07-10:单发会撞失败,iris 同代理靠 3 次重试稳定运行多日)——
        4xx 业务错误(如新合约无 OI 历史)不重试,直接抛给调用方分类处理。"""
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                async with self._s.get(
                    self._base + path, params=params, proxy=self._proxy
                ) as r:
                    r.raise_for_status()
                    return await r.json()
            except aiohttp.ClientResponseError:
                raise  # HTTP 状态错误 = 业务语义,重试无意义
            except aiohttp.ClientError as e:
                last = e
                await asyncio.sleep(1.0 * (attempt + 1))
        raise last  # type: ignore[misc]

    async def ticker_24h(self) -> list[dict]:
        return await self._get("/fapi/v1/ticker/24hr")

    async def klines(self, symbol: str, limit: int) -> list[dict]:
        raw = await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": CANDLE_INTERVAL, "limit": min(limit, 1500)},
        )
        return [
            {
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in raw
        ]

    async def funding_history(self, symbol: str, limit: int = 30) -> list[float]:
        raw = await self._get(
            "/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit}
        )
        return [float(r["fundingRate"]) for r in raw]

    async def funding_rate_now(self, symbol: str) -> Optional[float]:
        d = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        v = d.get("lastFundingRate")
        return float(v) if v is not None else None

    async def oi_change_pct(self, symbol: str) -> Optional[float]:
        """近 1h OI 变化百分比(openInterestHist 新上市/低关注度合约会 400/空,返回 None)。"""
        try:
            rows = await self._get(
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": "1h", "limit": 2},
            )
        except aiohttp.ClientResponseError:
            return None
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        a, b = float(rows[0]["sumOpenInterest"]), float(rows[-1]["sumOpenInterest"])
        return round((b - a) / a * 100.0, 4) if a > 0 else None

    async def long_ratio_pct(self, symbol: str) -> Optional[float]:
        """全局多空账户比 → 多头账户占比(0-100)。"""
        try:
            rows = await self._get(
                "/futures/data/globalLongShortAccountRatio",
                {"symbol": symbol, "period": "1h", "limit": 1},
            )
        except aiohttp.ClientResponseError:
            return None
        if not rows:
            return None
        return round(float(rows[0]["longAccount"]) * 100.0, 2)


# ── 服务主体 ──────────────────────────────────────────────────────────────────


@dataclass
class _SymbolState:
    closes: list[float] = field(default_factory=list)
    last_open_time: int = 0  # 最后一根已发布 K 线的 open_time
    last_raw_pub: float = 0.0  # 上次 market.raw 发布时刻(monotonic)
    change_pct_24h: Optional[float] = None


class MarketScanner:
    def __init__(self, client: BinanceScanClient) -> None:
        self._c = client
        self._states: dict[str, _SymbolState] = {}
        self._prev_volumes: dict[str, float] = {}
        self._discovered: list[str] = []
        self._interval_ms = _INTERVAL_MS[CANDLE_INTERVAL]

    @property
    def watchlist(self) -> list[str]:
        return CORE_SYMBOLS + [s for s in self._discovered if s not in CORE_SYMBOLS]

    # ── 发现 ────────────────────────────────────────────────────────────────
    async def scan_once(self) -> list[dict]:
        tickers = await self._c.ticker_24h()
        exclude = set(CORE_SYMBOLS)
        cands = discover_candidates(tickers, self._prev_volumes, exclude=exclude)
        # 全市场 24h ticker 缓存:量能激增基线 + 给 watchlist 提供 price_change_pct
        for t in tickers:
            sym = t.get("symbol", "")
            try:
                self._prev_volumes[sym] = float(t["quoteVolume"])
                if sym in self._states:
                    self._states[sym].change_pct_24h = float(t["priceChangePercent"])
            except (KeyError, ValueError, TypeError):
                continue
        self._discovered = [c["symbol"] for c in cands]
        r = get_redis()
        await r.set(
            "cw4:moonshots",
            json.dumps(
                [
                    {
                        **c,
                        "discovered_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    }
                    for c in cands
                ]
            ),
        )
        logger.info(
            "scan: %d candidates %s",
            len(cands),
            [(c["symbol"], c["change_pct"]) for c in cands],
        )
        return cands

    # ── 喂送 ────────────────────────────────────────────────────────────────
    async def _seed(self, symbol: str, st: _SymbolState) -> None:
        """新符号:回填 SEED_BARS 根 K 线热身 regime 检测器(backfill=true 不评分),
        最后一根已收盘 K 线留给 feed 主流程正常发布(触发首次评分)。"""
        klines = await self._c.klines(symbol, SEED_BARS + 1)
        now_ms = int(time.time() * 1000)
        closed = [k for k in klines if k["open_time"] + self._interval_ms <= now_ms]
        if len(closed) < 2:
            return
        seeds, last = closed[:-1], closed[-1]
        closes: list[float] = []
        for k in seeds:
            closes.append(k["close"])
            await publish(
                STREAM_MARKET_CANDLES,
                {
                    "symbol": symbol,
                    "backfill": True,
                    "open_time": k["open_time"],
                    "open": k["open"],
                    "high": k["high"],
                    "low": k["low"],
                    "close": k["close"],
                    "volume": k["volume"],
                    "indicators": compute_indicators(closes),
                },
            )
        st.closes = closes
        st.last_open_time = seeds[-1]["open_time"]
        logger.info(
            "seeded %s: %d backfill candles (last held for live scoring: %s)",
            symbol,
            len(seeds),
            last["open_time"],
        )

    async def _publish_raw(self, symbol: str, st: _SymbolState) -> None:
        fh = await self._c.funding_history(symbol)
        msg = {
            "symbol": symbol,
            "funding_rate": await self._c.funding_rate_now(symbol) or 0.0,
            "oi_change_pct": await self._c.oi_change_pct(symbol) or 0.0,
            "price_change_pct": st.change_pct_24h or 0.0,
            "long_ratio": await self._c.long_ratio_pct(symbol) or 50.0,
            "funding_history": fh,
        }
        await publish(STREAM_MARKET_RAW, msg)

    async def feed_once(self, symbol: str) -> None:
        st = self._states.setdefault(symbol, _SymbolState())
        if not st.closes:
            await self._seed(symbol, st)
            if not st.closes:
                return
            # 种子完成后先发 raw(funding/OI/LSR 就位),再让下面的常规路径
            # 发布最新收盘 K 线 → 首次评分即拥有全部因子。
            await self._publish_raw(symbol, st)
            st.last_raw_pub = time.monotonic()

        klines = await self._c.klines(symbol, 3)
        now_ms = int(time.time() * 1000)
        # 实时标记价 → cw4:prices(execution PAPER 撮合定价 + monitoring_loop 的
        # SL/TP 监控都读这个 hash;此前无人写,PAPER 只能按信号 entry_price 成交、
        # 止损止盈永远不触发)。取最后一根(成形中)K 线的最新 close ≈ 当前价,60s 刷新。
        if klines:
            r = get_redis()
            await r.hset(
                "cw4:prices", symbol, json.dumps({"price": klines[-1]["close"]})
            )
        k = latest_closed(klines, now_ms, self._interval_ms)
        if k and k["open_time"] > st.last_open_time:
            st.closes.append(k["close"])
            if len(st.closes) > 300:
                st.closes = st.closes[-300:]
            await publish(
                STREAM_MARKET_CANDLES,
                {
                    "symbol": symbol,
                    "open_time": k["open_time"],
                    "open": k["open"],
                    "high": k["high"],
                    "low": k["low"],
                    "close": k["close"],
                    "volume": k["volume"],
                    "indicators": compute_indicators(st.closes),
                },
            )
            st.last_open_time = k["open_time"]
            logger.info(
                "candle %s open_time=%s close=%s", symbol, k["open_time"], k["close"]
            )

        if time.monotonic() - st.last_raw_pub >= RAW_REFRESH_S:
            await self._publish_raw(symbol, st)
            st.last_raw_pub = time.monotonic()

    # ── 循环 ────────────────────────────────────────────────────────────────
    async def discovery_loop(self) -> None:
        while True:
            try:
                await self.scan_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("scan failed: %s: %s", type(e).__name__, e)
            await asyncio.sleep(SCAN_INTERVAL_S)

    async def feed_loop(self) -> None:
        while True:
            for symbol in self.watchlist:
                try:
                    await self.feed_once(symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "feed %s failed: %s: %s", symbol, type(e).__name__, e
                    )
            await asyncio.sleep(FEED_INTERVAL_S)


async def main() -> None:
    from shared.db.redis_client import init_redis

    init_redis(os.environ.get("REDIS_URL", "redis://helios-redis:6379/3"))
    timeout = aiohttp.ClientTimeout(
        total=float(os.environ.get("SCANNER_TIMEOUT_S", "20"))
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        scanner = MarketScanner(BinanceScanClient(session))
        logger.info(
            "market-scanner start: core=%s top_n=%d min_qvol=%.0f min_chg=%.1f%% proxy=%s",
            CORE_SYMBOLS,
            TOP_N,
            MIN_QUOTE_VOLUME_USD,
            MIN_24H_CHANGE_PCT,
            PROXY,
        )
        # 启动即扫一轮让 watchlist 立即有发现币;失败只降级(核心币照喂),
        # 不 crash-loop —— discovery_loop 5 分钟后自然重试。
        try:
            await scanner.scan_once()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "startup scan failed (will retry in loop): %s: %s", type(e).__name__, e
            )
        await asyncio.gather(scanner.discovery_loop(), scanner.feed_loop())


if __name__ == "__main__":
    asyncio.run(main())
