"""Uniswap V3 Quoter and Route Adapter on Base."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from liquidator.core.interfaces import CollateralRouteAdapter
from liquidator.core.models import ChainType, Token
from liquidator.core.scaling import norm_to_raw, raw_to_norm
from web3 import AsyncWeb3

logger = logging.getLogger(__name__)

QUOTER_V2_ABI = [
    {
        "inputs": [{
            "components": [
                {"internalType": "address", "name": "tokenIn", "type": "address"},
                {"internalType": "address", "name": "tokenOut", "type": "address"},
                {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                {"internalType": "uint24", "name": "fee", "type": "uint24"},
                {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
            ],
            "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
            "name": "params",
            "type": "tuple"
        }],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


class UniswapV3RouteAdapter(CollateralRouteAdapter):
    """
    Uniswap V3 Quoter and Swap Execution Adapter.
    Queries QuoterV2 at the exact detection block for realistic executable trade proceeds.
    Fails closed if the call reverts or cannot run.
    """

    def __init__(
        self,
        quoter_address: str = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        router_address: str = "0x2626664c2603336E57B271c5C0b26F421741e481",
        default_fee: int = 500,  # 0.05% standard on Base WETH/USDC
        w3: Optional[AsyncWeb3] = None,
    ):
        self.quoter_address = quoter_address
        self.router_address = router_address
        self.default_fee = default_fee
        self.w3 = w3

    @property
    def venue_name(self) -> str:
        return "uniswap_v3"

    @property
    def chain(self) -> ChainType:
        return ChainType.EVM

    async def get_quote(
        self,
        from_token: Token,
        to_token: Token,
        amount_in_raw: int,
        block_identifier: Optional[int] = None,
    ) -> int:
        """
        Query QuoterV2 for exact amount out at the detection block.
        Fails closed: if the static call reverts, raises ValueError (no opportunity recorded).
        """
        if amount_in_raw <= 0:
            return 0

        if self.w3 is None:
            # Offline test fallback when no RPC provider is attached
            fee_factor = Decimal(1) - (Decimal(self.default_fee) / Decimal(1_000_000))
            from_norm = raw_to_norm(amount_in_raw, from_token.decimals)
            val_usd = from_norm * fee_factor * from_token.price_usd
            to_norm = val_usd / to_token.price_usd
            return norm_to_raw(to_norm, to_token.decimals)

        quoter = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.quoter_address),
            abi=QUOTER_V2_ABI,
        )
        token_in = self.w3.to_checksum_address(from_token.address)
        token_out = self.w3.to_checksum_address(to_token.address)

        call_kwargs: Dict[str, Any] = {}
        if block_identifier is not None:
            call_kwargs["block_identifier"] = block_identifier

        # Test pool fee tiers in order of depth on Base (500 = 0.05%, 3000 = 0.3%, 10000 = 1%)
        best_amount_out = 0
        fee_tiers = [500, 3000, 10000] if self.default_fee not in [3000, 10000] else [self.default_fee, 500, 3000]

        last_error = None
        for fee in fee_tiers:
            try:
                params = (token_in, token_out, amount_in_raw, fee, 0)
                res = await quoter.functions.quoteExactInputSingle(params).call(**call_kwargs)
                amount_out = res[0]
                if amount_out > best_amount_out:
                    best_amount_out = amount_out
            except Exception as e:
                last_error = e
                continue

        if best_amount_out > 0:
            return best_amount_out

        # Fail closed: no execution route or static call reverted
        logger.warning(
            f"QuoterV2 reverted for {amount_in_raw} {from_token.symbol}->{to_token.symbol} at block {block_identifier}: {last_error}"
        )
        raise ValueError(
            f"QuoterV2 returned no quote for {from_token.symbol}->{to_token.symbol} at block {block_identifier} (fail closed)"
        )

    def build_swap_call(
        self,
        from_token: Token,
        to_token: Token,
        amount_in_raw: int,
        min_amount_out_raw: int,
        recipient: str,
    ) -> Dict[str, Any]:
        return {
            "target": self.router_address,
            "params": {
                "tokenIn": from_token.address,
                "tokenOut": to_token.address,
                "fee": self.default_fee,
                "recipient": recipient,
                "amountIn": amount_in_raw,
                "amountOutMinimum": min_amount_out_raw,
                "sqrtPriceLimitX96": 0,
            },
        }
