"""Aries Markets Adapter on Aptos (Move Architecture)."""

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


class AriesAdapter(LendingProtocolAdapter):
    """
    Aries Markets adapter on Aptos.
    Architecture: Move module entry function.
    Status: RESEARCHED.
    """

    def __init__(self, module_address: str = "0x9770fa9c725ac97da0da9330b4a77189096d9e2848f47825721f2aa8e6f0cb2"):
        self.module_address = module_address
        self._status = AdapterStatus.RESEARCHED

    @property
    def protocol_id(self) -> str:
        return "aries_aptos"

    @property
    def chain(self) -> ChainType:
        return ChainType.APTOS

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
        bonus = Decimal("1.05")

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
            "function": f"{self.module_address}::controller::liquidate",
            "type_arguments": [collateral_asset.address, debt_asset.address],
            "arguments": [borrower_address, str(repay_amount_raw)],
        }
