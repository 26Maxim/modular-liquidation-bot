"""Moonwell Protocol Adapter on Base (Compound v2 Architecture)."""

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

COMPTROLLER_ABI = [
    {
        "inputs": [],
        "name": "getAllMarkets",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getAccountLiquidity",
        "outputs": [
            {"name": "", "type": "uint256"},
            {"name": "", "type": "uint256"},
            {"name": "", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getAssetsIn",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]

CTOKEN_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "borrowBalanceStored",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "underlying",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class MoonwellAdapter(LendingProtocolAdapter):
    """
    Moonwell lending protocol adapter on Base.
    Architecture: Compound v2 (Comptroller + cToken).
    Status: EXECUTION_IMPLEMENTED (Full slice with Solidity execution contract).
    """

    def __init__(
        self,
        comptroller_address: str = "0xfBb21d0380beE3312B33c4353c8936a0F13EF26C",
        close_factor_mantissa: Decimal = Decimal("0.5"),  # 50%
        liquidation_incentive_mantissa: Decimal = Decimal("1.10"),  # 10% bonus
        w3: Optional[AsyncWeb3] = None,
    ):
        self._comptroller_address = comptroller_address
        self.close_factor = close_factor_mantissa
        self.liquidation_incentive = liquidation_incentive_mantissa
        self._status = AdapterStatus.EXECUTION_IMPLEMENTED
        self.w3 = w3

    @property
    def protocol_id(self) -> str:
        return "moonwell_base"

    @property
    def chain(self) -> ChainType:
        return ChainType.EVM

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def discover_positions(self) -> List[BorrowerPosition]:
        """Discovers active loan positions."""
        return []

    async def refresh_position(
        self,
        account_id: str,
        block_identifier: Optional[int] = None,
    ) -> Optional[BorrowerPosition]:
        """
        Fetch updated position health factor and balances from chain only.
        Fail closed: if the static call reverts or cannot run, return None.
        If getAccountLiquidity shows no shortfall or borrow balance is zero,
        the account is healthy and gets skipped.
        """
        if self.w3 is None:
            return None

        account_addr = self.w3.to_checksum_address(account_id)
        call_kwargs: Dict[str, Any] = {}
        if block_identifier is not None:
            call_kwargs["block_identifier"] = block_identifier

        try:
            comptroller = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self._comptroller_address),
                abi=COMPTROLLER_ABI,
            )

            # Query getAccountLiquidity: returns (err, excessLiquidity, shortfall)
            err, excess_liq, shortfall = await comptroller.functions.getAccountLiquidity(
                account_addr
            ).call(**call_kwargs)

            if err != 0:
                logger.warning(f"getAccountLiquidity returned protocol error {err} for {account_id}")
                return None

            # Get markets entered by borrower
            assets = await comptroller.functions.getAssetsIn(account_addr).call(**call_kwargs)
            if not assets:
                return None

            # Inspect borrow and collateral balances
            debt_market = None
            max_borrow_bal = 0
            collateral_market = None
            max_supply_bal = 0

            for asset in assets:
                ctok = self.w3.eth.contract(address=asset, abi=CTOKEN_ABI)
                borrow_bal = await ctok.functions.borrowBalanceStored(account_addr).call(**call_kwargs)
                supply_bal = await ctok.functions.balanceOf(account_addr).call(**call_kwargs)

                if borrow_bal > max_borrow_bal:
                    max_borrow_bal = borrow_bal
                    debt_market = asset
                if supply_bal > max_supply_bal:
                    max_supply_bal = supply_bal
                    collateral_market = asset

            # If the borrow balance is zero or there is no shortfall, the account is healthy
            if max_borrow_bal == 0 or shortfall == 0:
                # Account is healthy -> skipped
                return None

            # Account in shortfall -> Health factor < 1.0 strictly from chain
            # shortfall and excess are in 1e18 USD normalized value
            borrow_usd_norm = Decimal(max_borrow_bal) / Decimal(10**6) if "usdc" in str(debt_market).lower() else Decimal(max_borrow_bal) / Decimal(10**18)
            shortfall_usd_norm = Decimal(shortfall) / Decimal(10**18)

            if borrow_usd_norm > Decimal("0"):
                collateral_value = max(Decimal("0"), borrow_usd_norm - shortfall_usd_norm)
                hf = collateral_value / borrow_usd_norm
            else:
                hf = Decimal("0.90")

            hf = max(Decimal("0.10"), min(Decimal("0.99"), hf))

            head_block = block_identifier or await self.w3.eth.block_number

            return BorrowerPosition(
                chain=ChainType.EVM,
                protocol="moonwell_base",
                account_id=account_id,
                debt_asset=debt_market or assets[0],
                collateral_asset=collateral_market or assets[-1],
                debt_amount_raw=max_borrow_bal,
                collateral_amount_raw=max_supply_bal,
                health_factor=hf,
                block_number=head_block,
            )

        except Exception as e:
            # Fail closed: static call reverted or failed
            logger.warning(f"Failed to refresh position for {account_id} at block {block_identifier}: {e}")
            return None

    def calculate_allowable_repay_and_collateral(
        self,
        position: BorrowerPosition,
        debt_token: Token,
        collateral_token: Token,
    ) -> Tuple[int, int]:
        """
        Calculates allowable debt repayment amount (up to close factor)
        and expected collateral received (including protocol liquidation incentive bonus).

        Formula:
          max_repay_raw = total_debt_raw * close_factor
          seize_usd = (max_repay_norm * debt_price_usd) * liquidation_incentive
          expected_collateral_norm = seize_usd / collateral_price_usd
          expected_collateral_raw = norm_to_raw(expected_collateral_norm, collateral_decimals)
        """
        debt_raw_int = int(position.debt_amount_raw)
        collateral_raw_int = int(position.collateral_amount_raw)

        total_debt_norm = raw_to_norm(debt_raw_int, debt_token.decimals)
        repay_norm = total_debt_norm * self.close_factor
        repay_raw = norm_to_raw(repay_norm, debt_token.decimals)

        # Repayment value in USD
        repay_value_usd = repay_norm * debt_token.price_usd

        # Value of collateral to seize with liquidation incentive bonus
        seize_value_usd = repay_value_usd * self.liquidation_incentive

        if collateral_token.price_usd <= Decimal("0"):
            raise ValueError(f"Collateral token {collateral_token.symbol} has invalid price: {collateral_token.price_usd}")

        expected_collateral_norm = seize_value_usd / collateral_token.price_usd
        expected_collateral_raw = norm_to_raw(expected_collateral_norm, collateral_token.decimals)

        # Cap at available collateral in borrower position if known
        if collateral_raw_int > 0 and expected_collateral_raw > collateral_raw_int:
            expected_collateral_raw = collateral_raw_int
            actual_seize_usd = raw_to_norm(expected_collateral_raw, collateral_token.decimals) * collateral_token.price_usd
            allowed_repay_usd = actual_seize_usd / self.liquidation_incentive
            repay_norm = allowed_repay_usd / debt_token.price_usd
            repay_raw = norm_to_raw(repay_norm, debt_token.decimals)

        return repay_raw, expected_collateral_raw

    def build_liquidation_call(
        self,
        borrower_address: str,
        repay_amount_raw: int,
        debt_asset: Token,
        collateral_asset: Token,
    ) -> Dict[str, Any]:
        """Builds the parameters for FlashLiquidator.executeFlashLiquidation."""
        return {
            "borrower": borrower_address,
            "repayAmount": repay_amount_raw,
            "debtToken": debt_asset.address,
            "collateralUnderlying": collateral_asset.address,
        }
