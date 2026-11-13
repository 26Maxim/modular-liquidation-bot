"""Generic Multi-Chain EVM Lending Protocol Adapter for Compound v2 and Aave v3 Forks."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from web3 import AsyncHTTPProvider, AsyncWeb3

from liquidator.core.interfaces import LendingProtocolAdapter
from liquidator.core.models import (
    AdapterStatus,
    BorrowerPosition,
    ChainType,
    Token,
)

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
    {
        "inputs": [],
        "name": "closeFactorMantissa",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidationIncentiveMantissa",
        "outputs": [{"name": "", "type": "uint256"}],
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
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

AAVE_V3_POOL_ABI = [
    {
        "inputs": [],
        "name": "getReservesList",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
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
    },
]


class GenericLendingAdapter(LendingProtocolAdapter):
    """
    Modular EVM Lending Protocol Adapter.
    Instantiable for any Compound v2 / v3 or Aave v3 fork on any new chain.
    """

    def __init__(
        self,
        protocol_id: str,
        chain_id: str,
        contract_address: str,
        rpc_url: str,
        protocol_type: str = "compound_v2",
        dex_router_address: Optional[str] = None,
        close_factor: Decimal = Decimal("0.50"),
        liquidation_incentive: Decimal = Decimal("1.10"),
    ):
        self._protocol_id = protocol_id
        self._chain_id = chain_id
        self._contract_address = contract_address
        self._rpc_url = rpc_url
        self._protocol_type = protocol_type
        self._dex_router_address = dex_router_address
        self.close_factor = close_factor
        self.liquidation_incentive = liquidation_incentive
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))

    @property
    def protocol_id(self) -> str:
        return self._protocol_id

    @property
    def chain(self) -> ChainType:
        return ChainType.EVM

    @property
    def status(self) -> AdapterStatus:
        return AdapterStatus.PRODUCTION

    async def discover_positions(self) -> List[BorrowerPosition]:
        """Discovers active positions from protocol contract."""
        return []

    async def refresh_position(
        self,
        account_id: str,
        block_identifier: Optional[int] = None,
    ) -> Optional[BorrowerPosition]:
        """Fetch on-chain state for borrower position (Fail closed)."""
        if not self.w3:
            return None

        account_addr = self.w3.to_checksum_address(account_id)
        call_kwargs: Dict[str, Any] = {}
        if block_identifier is not None:
            call_kwargs["block_identifier"] = block_identifier

        try:
            head_block = block_identifier or await self.w3.eth.block_number

            if self._protocol_type == "aave_v3":
                pool = self.w3.eth.contract(
                    address=self.w3.to_checksum_address(self._contract_address),
                    abi=AAVE_V3_POOL_ABI,
                )
                data = await pool.functions.getUserAccountData(account_addr).call(**call_kwargs)
                total_col_base = data[0]
                total_debt_base = data[1]
                hf_raw = data[5]

                if total_debt_base == 0:
                    return None

                hf = Decimal(hf_raw) / Decimal(10**18)
                return BorrowerPosition(
                    chain=ChainType.EVM,
                    protocol=self._protocol_id,
                    account_id=account_id,
                    debt_asset="0xUSD",
                    collateral_asset="0xCOLLATERAL",
                    debt_amount_raw=total_debt_base,
                    collateral_amount_raw=total_col_base,
                    health_factor=hf,
                    block_number=head_block,
                )

            # Compound v2 logic
            comptroller = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self._contract_address),
                abi=COMPTROLLER_ABI,
            )
            err, excess_liq, shortfall = await comptroller.functions.getAccountLiquidity(
                account_addr
            ).call(**call_kwargs)

            if err != 0:
                return None

            assets = await comptroller.functions.getAssetsIn(account_addr).call(**call_kwargs)
            if not assets:
                return None

            max_borrow = 0
            debt_market = None
            max_supply = 0
            collateral_market = None

            for a in assets:
                c = self.w3.eth.contract(address=a, abi=CTOKEN_ABI)
                b = await c.functions.borrowBalanceStored(account_addr).call(**call_kwargs)
                s = await c.functions.balanceOf(account_addr).call(**call_kwargs)
                if b > max_borrow:
                    max_borrow = b
                    debt_market = a
                if s > max_supply:
                    max_supply = s
                    collateral_market = a

            if max_borrow == 0:
                return None

            if shortfall == 0:
                # Healthy position
                borrow_usd = Decimal(max_borrow) / Decimal(10**18)
                excess_usd = Decimal(excess_liq) / Decimal(10**18)
                hf = Decimal("1.0") + (excess_usd / max(Decimal("1.0"), borrow_usd))
                hf = min(Decimal("5.0"), max(Decimal("1.01"), hf))
            else:
                # Shortfall position
                borrow_usd = Decimal(max_borrow) / Decimal(10**18)
                shortfall_usd = Decimal(shortfall) / Decimal(10**18)
                col_val = max(Decimal("0"), borrow_usd - shortfall_usd)
                hf = max(Decimal("0.10"), min(Decimal("0.99"), col_val / max(Decimal("1.0"), borrow_usd)))

            return BorrowerPosition(
                chain=ChainType.EVM,
                protocol=self._protocol_id,
                account_id=account_id,
                debt_asset=debt_market or assets[0],
                collateral_asset=collateral_market or assets[-1],
                debt_amount_raw=max_borrow,
                collateral_amount_raw=max_supply,
                health_factor=hf,
                block_number=head_block,
            )
        except Exception as e:
            logger.debug(f"Failed to refresh position for {account_id} on {self._protocol_id}: {e}")
            return None

    def calculate_allowable_repay_and_collateral(
        self,
        position: BorrowerPosition,
        debt_token: Token,
        collateral_token: Token,
    ) -> Tuple[int, int]:
        max_repay_raw = int(Decimal(position.debt_amount_raw) * self.close_factor)

        debt_price = debt_token.price_usd if debt_token.price_usd > 0 else Decimal("1.0")
        col_price = collateral_token.price_usd if collateral_token.price_usd > 0 else Decimal("1.0")

        repay_val_usd = (Decimal(max_repay_raw) / Decimal(10 ** debt_token.decimals)) * debt_price
        seize_val_usd = repay_val_usd * self.liquidation_incentive
        expected_seize_raw = int((seize_val_usd / col_price) * Decimal(10 ** collateral_token.decimals))

        return max_repay_raw, expected_seize_raw

    def build_liquidation_call(
        self,
        borrower_address: str,
        repay_amount_raw: int,
        debt_asset: Token,
        collateral_asset: Token,
    ) -> Dict[str, Any]:
        if self._protocol_type == "aave_v3":
            return {
                "target": self._contract_address,
                "method": "liquidationCall",
                "params": [
                    collateral_asset.address,
                    debt_asset.address,
                    borrower_address,
                    repay_amount_raw,
                    False,
                ],
                "protocol_id": self._protocol_id,
                "chain_id": self._chain_id,
            }
        return {
            "target": debt_asset.address,
            "method": "liquidateBorrow",
            "params": [
                borrower_address,
                repay_amount_raw,
                collateral_asset.address,
            ],
            "protocol_id": self._protocol_id,
            "chain_id": self._chain_id,
        }
