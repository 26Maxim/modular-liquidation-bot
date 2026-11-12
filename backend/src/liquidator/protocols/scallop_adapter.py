"""Scallop Protocol Adapter on Sui."""

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


class ScallopAdapter(LendingProtocolAdapter):
    """
    Scallop lending protocol adapter on Sui.
    Architecture: Sui Move package with shared Market and Obligation objects.
    Status: READ_ONLY.
    """

    def __init__(self, package_id: str = "0xefe8b36d5b2e43728cc323298633b83028110d9d61c4c04e53670617f6e6330"):
        self.package_id = package_id
        self._status = AdapterStatus.READ_ONLY

    @property
    def protocol_id(self) -> str:
        return "scallop_sui"

    @property
    def chain(self) -> ChainType:
        return ChainType.SUI

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
        close_factor = Decimal("0.50")
        bonus = Decimal("1.07")  # 7% liquidation bonus

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
            "target": f"{self.package_id}::liquidate::liquidate",
            "type_arguments": [collateral_asset.address, debt_asset.address],
            "arguments": [
                "MarketSharedObject",
                borrower_address,
                repay_amount_raw,
                "ClockSharedObject",
            ],
        }
