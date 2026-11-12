"""Seamless Protocol Adapter on Base (Aave v3 Architecture)."""

from __future__ import annotations

import logging
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
from web3 import AsyncWeb3

logger = logging.getLogger(__name__)

SEAMLESS_POOL_ABI = [
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]


class SeamlessAdapter(LendingProtocolAdapter):
    """
    Seamless Protocol adapter on Base.
    Architecture: Aave v3 fork (IPool liquidationCall).
    Status: READ_ONLY / SIMULATION_ONLY.
    """

    def __init__(
        self,
        pool_address: str = "0x8F44Fd754285aa6A2b8B9B97739B79746e0475a7",
        w3: Optional[AsyncWeb3] = None,
    ):
        self.pool_address = pool_address
        self.w3 = w3
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

    async def refresh_position(
        self,
        account_id: str,
        block_identifier: Optional[int] = None,
    ) -> Optional[BorrowerPosition]:
        """
        Fetch updated position health factor directly from Seamless Pool on-chain.
        Fail closed: if call reverts or user has 0 debt, return None.
        """
        if self.w3 is None:
            return None

        account_addr = self.w3.to_checksum_address(account_id)
        call_kwargs: Dict[str, Any] = {}
        if block_identifier is not None:
            call_kwargs["block_identifier"] = block_identifier

        try:
            pool = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self.pool_address),
                abi=SEAMLESS_POOL_ABI,
            )
            res = await pool.functions.getUserAccountData(account_addr).call(**call_kwargs)
            collateral_base = res[0]
            debt_base = res[1]
            raw_hf = res[5]

            # If no debt, user is not a borrower
            if debt_base == 0:
                return None

            # Aave v3 health factor in 1e18 decimals
            hf = Decimal(raw_hf) / Decimal(10**18)

            head_block = block_identifier or await self.w3.eth.block_number

            return BorrowerPosition(
                chain=ChainType.EVM,
                protocol="seamless_base",
                account_id=account_id,
                debt_asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC default base unit
                collateral_asset="0x4200000000000000000000000000000000000006",  # WETH
                debt_amount_raw=debt_base,
                collateral_amount_raw=collateral_base,
                health_factor=hf,
                block_number=head_block,
                metadata={
                    "total_collateral_base": str(collateral_base),
                    "total_debt_base": str(debt_base),
                },
            )

        except Exception as e:
            logger.debug(f"Seamless refresh_position failed for {account_id}: {e}")
            return None

    def calculate_allowable_repay_and_collateral(
        self, position: BorrowerPosition, debt_token: Token, collateral_token: Token
    ) -> Tuple[int, int]:
        # Aave v3 close factor: 50% if HF > 0.95, 100% if HF <= 0.95
        close_factor = Decimal("1.0") if position.health_factor <= Decimal("0.95") else Decimal("0.5")
        bonus = Decimal("1.05")  # 5% bonus default

        debt_norm = raw_to_norm(int(position.debt_amount_raw), debt_token.decimals)
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
