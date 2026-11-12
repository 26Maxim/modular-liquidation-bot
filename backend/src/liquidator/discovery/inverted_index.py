"""Inverted Index & Liquidation Threshold Engine for Zero-Latency Opportunity Matching."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from liquidator.core.models import BorrowerPosition, ChainType

logger = logging.getLogger(__name__)

# Fallback reference prices if live oracle feed is warming up
DEFAULT_ASSET_PRICES: Dict[str, Tuple[str, Decimal]] = {
    # EVM (Base)
    "0x4200000000000000000000000000000000000006": ("WETH", Decimal("2046.52")),
    "0x628ff693426583D9a7FB391E54366292F509D457": ("mWETH", Decimal("2046.52")),
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": ("USDC", Decimal("1.00")),
    "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22": ("mUSDC", Decimal("1.00")),
    "0x703843C3379b52F9FF486c9f5892218d2a065cC8": ("mUSDC", Decimal("1.00")),
    "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": ("cbBTC", Decimal("73516.28")),
    "0xF877ACaFA28c19b96727966690b2f44d35aD5976": ("mcbBTC", Decimal("73516.28")),
    "0x10fF57877b79e9bd949B3815220eC87B9fc5D2ee": ("mLBTC", Decimal("73500.00")),
    "0xdC7810B47eAAb250De623F0eE07764afa5F71ED1": ("mWELL", Decimal("0.045")),
    "0x6308204872BdB7432dF97b04B42443c714904F3E": ("mMORPHO", Decimal("1.85")),
    # Solana
    "So11111111111111111111111111111111111111112": ("SOL", Decimal("135.00")),
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", Decimal("1.00")),
    # Sui
    "0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI": ("SUI", Decimal("2.15")),
    "0x2::sui::SUI": ("SUI", Decimal("2.15")),
    "dba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC": ("USDC", Decimal("1.00")),
}


class InvertedIndexEngine:
    """
    In-memory Inverted Index tracking debt positions bucketed by collateral asset.
    Pre-computes exact Liquidation Price (P_liq) and assigns monitoring tiers:
      - HOT: HF <= 1.10 (within 9.1% of liquidation) -> high priority, checked block-by-block.
      - WARM: 1.10 < HF <= 1.30 (within 23% of liquidation) -> checked periodically.
      - COLD: HF > 1.30 -> zero per-block RPC calls, sleeps until oracle price drops.
    """

    def __init__(self):
        # Index: collateral_asset_address_lower -> List[BorrowerPosition] sorted by health_factor asc
        self._collateral_index: Dict[str, List[BorrowerPosition]] = {}
        # Fast key lookup: id -> BorrowerPosition
        self._positions_by_id: Dict[str, BorrowerPosition] = {}
        # Live prices cache: asset_address_lower -> Decimal price
        self._live_prices: Dict[str, Decimal] = {}

    def update_price(self, asset_address: str, price_usd: Decimal) -> List[BorrowerPosition]:
        """
        Record a live oracle price update and return any positions that have breached
        their liquidation price (sub-millisecond instant memory lookup).
        """
        key = asset_address.lower()
        self._live_prices[key] = price_usd

        breached: List[BorrowerPosition] = []
        positions = self._collateral_index.get(key, [])
        for pos in positions:
            # Recompute metric with new price
            self._enrich_position(pos, price_override=price_usd)
            if pos.liquidation_price_usd and price_usd <= pos.liquidation_price_usd:
                breached.append(pos)

        return breached

    def get_asset_info(self, asset_address: str) -> Tuple[str, Decimal]:
        key = asset_address.lower()
        if key in self._live_prices:
            # Look up symbol
            for def_addr, (sym, _) in DEFAULT_ASSET_PRICES.items():
                if def_addr.lower() == key:
                    return sym, self._live_prices[key]
            return "TOKEN", self._live_prices[key]

        for def_addr, (sym, pr) in DEFAULT_ASSET_PRICES.items():
            if def_addr.lower() == key or def_addr.lower().endswith(key[-20:]):
                return sym, pr

        if "usdc" in key or "usd" in key:
            return "USDC", Decimal("1.00")
        if "eth" in key:
            return "ETH", Decimal("2046.52")
        if "btc" in key:
            return "BTC", Decimal("73516.28")
        if "sol" in key:
            return "SOL", Decimal("135.00")
        if "sui" in key:
            return "SUI", Decimal("2.15")

        return "ASSET", Decimal("1.00")

    def _enrich_position(self, pos: BorrowerPosition, price_override: Optional[Decimal] = None) -> BorrowerPosition:
        """Calculate Liquidation Price, Distance %, and Monitoring Tier."""
        col_sym, col_price = self.get_asset_info(pos.collateral_asset)
        debt_sym, _ = self.get_asset_info(pos.debt_asset)

        if price_override is not None:
            col_price = price_override

        pos.collateral_symbol = col_sym
        pos.debt_symbol = debt_sym

        hf = pos.health_factor
        if hf and hf > 0:
            # P_liq = P_curr / HF
            pos.liquidation_price_usd = round(col_price / hf, 4)
            # Distance to liquidation = (1 - 1/HF) * 100%
            dist = (Decimal("1.0") - (Decimal("1.0") / hf)) * Decimal("100.0")
            pos.distance_to_liq_pct = round(dist, 2)

            # Assign Tier
            if hf <= Decimal("1.10"):
                pos.tier = "HOT"
            elif hf <= Decimal("1.30"):
                pos.tier = "WARM"
            else:
                pos.tier = "COLD"
        else:
            pos.liquidation_price_usd = col_price
            pos.distance_to_liq_pct = Decimal("0.00")
            pos.tier = "HOT"

        return pos

    def index_position(self, pos: BorrowerPosition) -> BorrowerPosition:
        """Enrich position with P_liq and tier, then insert into the inverted index."""
        enriched = self._enrich_position(pos)
        pos_id = enriched.id or f"{enriched.chain.value}_{enriched.protocol}_{enriched.account_id}"
        enriched.id = pos_id

        self._positions_by_id[pos_id] = enriched

        col_key = enriched.collateral_asset.lower()
        if col_key not in self._collateral_index:
            self._collateral_index[col_key] = []

        # Remove existing if present
        self._collateral_index[col_key] = [
            p for p in self._collateral_index[col_key] if p.id != pos_id
        ]
        self._collateral_index[col_key].append(enriched)
        # Keep sorted by health factor ascending (lowest HF first)
        self._collateral_index[col_key].sort(key=lambda p: p.health_factor)

        return enriched

    def get_hot_positions(self) -> List[BorrowerPosition]:
        """Returns all positions currently in the HOT monitoring tier."""
        return [p for p in self._positions_by_id.values() if p.tier == "HOT"]

    def get_summary(self) -> Dict[str, Any]:
        """Calculates aggregated metrics for dashboard summary widgets."""
        all_positions = list(self._positions_by_id.values())
        total = len(all_positions)

        hot_count = sum(1 for p in all_positions if p.tier == "HOT")
        warm_count = sum(1 for p in all_positions if p.tier == "WARM")
        cold_count = sum(1 for p in all_positions if p.tier == "COLD")

        # Closest position to shortfall
        closest = None
        if all_positions:
            sorted_by_hf = sorted(all_positions, key=lambda p: p.health_factor)
            closest_pos = sorted_by_hf[0]
            closest = {
                "account_id": closest_pos.account_id,
                "chain": closest_pos.chain.value,
                "protocol": closest_pos.protocol,
                "health_factor": str(closest_pos.health_factor),
                "collateral_symbol": closest_pos.collateral_symbol or "ETH",
                "liquidation_price_usd": str(closest_pos.liquidation_price_usd or 0),
                "distance_to_liq_pct": str(closest_pos.distance_to_liq_pct or 0),
                "tier": closest_pos.tier,
            }

        return {
            "total_monitored": total,
            "hot_count": hot_count,
            "warm_count": warm_count,
            "cold_count": cold_count,
            "assets_indexed": len(self._collateral_index),
            "closest_to_shortfall": closest,
        }

    def query(
        self,
        chain: Optional[str] = None,
        tier: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = "health_factor",
        page: int = 1,
        page_size: int = 10,
    ) -> Dict[str, Any]:
        """Filtered and paginated query over indexed positions."""
        results = list(self._positions_by_id.values())

        if chain and chain.upper() != "ALL":
            results = [p for p in results if p.chain.value == chain.upper()]

        if tier and tier.upper() != "ALL":
            results = [p for p in results if p.tier == tier.upper()]

        if search:
            s = search.lower().strip()
            results = [
                p for p in results
                if s in p.account_id.lower()
                or s in (p.collateral_symbol or "").lower()
                or s in (p.debt_symbol or "").lower()
                or s in p.protocol.lower()
            ]

        # Sorting logic
        if sort_by == "last_updated":
            results.sort(key=lambda p: p.last_updated, reverse=True)
        elif sort_by == "distance":
            results.sort(key=lambda p: float(p.distance_to_liq_pct if p.distance_to_liq_pct is not None else 999.0))
        else:
            # Default: sort by health factor ascending (most vulnerable first)
            results.sort(key=lambda p: p.health_factor)

        total_count = len(results)
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size

        paginated_items = results[start_idx:end_idx]

        return {
            "items": [p.model_dump() for p in paginated_items],
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
