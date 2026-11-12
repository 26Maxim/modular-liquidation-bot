"""Seamless Protocol Adapter on Base (Aave v3 Architecture)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from liquidator.core.interfaces import LendingProtocolAdapter
from liquidator.core.models import (
    AdapterStatus,
    BorrowerPosition,
    ChainType,
    Token,
)
from liquidator.core.scaling import norm_to_raw, raw_to_norm


class SeamlessAdapter(LendingProtocolAdapter):
    """
    Seamless Protocol adapter on Base.
    Architecture: Aave v3 fork (IPool liquidationCall).
    Status: READ_ONLY / SIMULATION_ONLY.
    """

    def __init__(self, pool_address: str = "0x8f44Fd754285aa6A2b8B9B97739B79746e0475a7"):
        self.pool_address = pool_address
        self._status = AdapterStatus.READ_ONLY

    @property
    def protocol_id(self) -> str:
        return "seamless_base"

    @property
    def chain(self) -> ChainType:
        return ChainType.EVM

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def discover_positions(self) -> List[BorrowerPosition]:
        return []

    async def refresh_position(self, account_id: str) -> Optional[BorrowerPosition]:
        return None

    def calculate_allowable_repay_and_collateral(
        self, position: BorrowerPosition, debt_token: Token, collateral_token: Token
    ) -> Tuple[int, int]:
        # Aave v3 close factor: 50% if HF > 0.95, 100% if HF <= 0.95
        close_factor = Decimal("1.0") if position.health_factor <= Decimal("0.95") else Decimal("0.5")
        bonus = Decimal("1.05")  # 5% bonus default

        debt_norm = raw_to_norm(position.debt_amount_raw, debt_token.decimals)
        repay_norm = debt_norm * close_factor
        repay_raw = norm_to_raw(repay_norm, debt_token.decimals)

        repay_usd = repay_norm * debt_token.price_usd
        seize_usd = repay_usd * bonus
        collateral_norm = seize_usd / collateral_token.price_usd
        collateral_raw = norm_to_raw(collateral_norm, collateral_token.decimals)

        return repay_raw, collateral_raw

    def build_liquidation_call(
        self,
        borrower_address: str,
        repay_amount_raw: int,
        debt_asset: Token,
        collateral_asset: Token,
    ) -> Dict[str, Any]:
        return {
            "target": self.pool_address,
            "method": "liquidationCall",
            "params": [
                collateral_asset.address,
                debt_asset.address,
                borrower_address,
                repay_amount_raw,
                False,  # receive underlying instead of aToken
            ],
        }
