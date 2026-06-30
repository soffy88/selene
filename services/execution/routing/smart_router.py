"""
services/execution/routing/smart_router.py

Smart Order Router：
  - 同时查询所有已注册 Adapter 的实时盘口
  - 基于预期成交成本（滑点 + 手续费）选择最优交易所
  - 支持跨交易所拆单（大单分批，减少冲击成本）
  - 失败自动 Fallback 到次优交易所
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Optional

from services.execution.adapters.base import BaseAdapter, OrderResult, get_all_adapters

logger = logging.getLogger("execution.router")

# 各交易所手续费（Maker/Taker）
FEES = {
    "binance": {"maker": 0.0002, "taker": 0.0004},
    "okx":     {"maker": 0.0002, "taker": 0.0005},
}

# 最大拆单数量（防止订单过碎）
MAX_SPLITS = 3


@dataclass
class RouteResult:
    adapter_name: str
    expected_cost_pct: float   # 总预期成本（滑点 + 手续费）
    available_depth:   float   # 盘口可用深度（USD）
    spread_pct:        float


@dataclass
class ExecutionPlan:
    routes:    list[RouteResult]   # 排序后的路由候选
    splits:    list[dict]          # 拆单计划 [{adapter, qty, pct}]
    total_qty: float


class SmartRouter:

    def __init__(self, max_participation: float = 0.10):
        """
        max_participation: 单次下单不超过盘口深度的 N%（默认10%）
        超过时自动拆单。
        """
        self._max_part = max_participation

    async def route(
        self,
        symbol:    str,
        side:      str,
        qty_usd:   float,
        order_type: str = "LIMIT",
    ) -> ExecutionPlan:
        """
        分析所有可用交易所，返回最优执行计划。
        """
        adapters = get_all_adapters()
        if not adapters:
            raise RuntimeError("No adapters registered")

        # 并发查询所有交易所盘口
        tasks = {
            name: asyncio.create_task(adapter.get_orderbook(symbol, depth=20))
            for name, adapter in adapters.items()
        }
        books = {}
        for name, task in tasks.items():
            try:
                books[name] = await task
            except Exception as e:
                logger.warning(f"orderbook {name}: {e}")

        routes = []
        for name, book in books.items():
            route = self._analyze_book(name, book, side, qty_usd, order_type)
            if route:
                routes.append(route)

        if not routes:
            # fallback: 使用第一个适配器，不做分析
            name = list(adapters.keys())[0]
            routes = [RouteResult(name, 0.001, qty_usd, 0.001)]

        # 按预期成本排序（低→高）
        routes.sort(key=lambda r: r.expected_cost_pct)

        # 拆单计划
        splits = self._plan_splits(routes, qty_usd)

        return ExecutionPlan(routes=routes, splits=splits, total_qty=qty_usd)

    def _analyze_book(
        self,
        name: str,
        book: dict,
        side: str,
        qty_usd: float,
        order_type: str,
    ) -> Optional[RouteResult]:
        try:
            levels = book.get("asks" if side == "BUY" else "bids", [])
            if not levels:
                return None

            best_price = levels[0][0]
            # 计算可用深度（前5档）
            depth_usd = sum(p * q for p, q in levels[:5])

            # 价格冲击：吃掉 qty_usd 需要的加权均价 vs 最优价。
            # avg_price = 实际花费USD / 实际买到的base数量。必须分开累计base数量
            # （take/price），否则 notional==consumed 会让 avg_price 恒等于 best_price、
            # impact 恒为 0，路由就只剩价差+手续费、跨所拆单失效。
            consumed = 0.0       # USD filled
            base_qty = 0.0       # base units acquired
            for price, qty in levels:
                level_usd = price * qty
                take = min(level_usd, qty_usd - consumed)
                if take <= 0:
                    break
                base_qty  += take / price
                consumed  += take
                if consumed >= qty_usd:
                    break
            avg_price  = consumed / base_qty if base_qty > 0 else best_price
            impact_pct = abs(avg_price - best_price) / best_price

            # 买卖价差
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                spread_pct = (asks[0][0] - bids[0][0]) / asks[0][0]
            else:
                spread_pct = 0.001

            fee_rate = FEES.get(name, {}).get(
                "maker" if order_type == "LIMIT" else "taker", 0.0004
            )

            total_cost = (spread_pct / 2) + impact_pct + fee_rate

            return RouteResult(
                adapter_name=name,
                expected_cost_pct=round(total_cost, 6),
                available_depth=round(depth_usd, 2),
                spread_pct=round(spread_pct, 6),
            )
        except Exception as e:
            logger.warning(f"analyze_book {name}: {e}")
            return None

    def _plan_splits(self, routes: list[RouteResult], total_qty_usd: float) -> list[dict]:
        """
        如果单交易所深度不足，跨交易所拆单。
        简单策略：优先用深度最大的交易所，超出部分分配到次优。
        """
        splits = []
        remaining = total_qty_usd

        for route in routes[:MAX_SPLITS]:
            if remaining <= 0:
                break
            # 分配不超过该交易所可用深度的 max_participation 比例
            max_here = route.available_depth * self._max_part
            alloc = min(remaining, max_here, remaining)  # 保守取最小
            if alloc < total_qty_usd * 0.05:   # 低于5%不拆，忽略
                continue
            splits.append({
                "adapter":  route.adapter_name,
                "qty_usd":  round(alloc, 2),
                "pct":      round(alloc / total_qty_usd, 4),
                "cost_est": route.expected_cost_pct,
            })
            remaining -= alloc

        # 如果还有剩余，全给最优交易所
        if remaining > 1 and splits:
            splits[0]["qty_usd"] += remaining

        return splits
