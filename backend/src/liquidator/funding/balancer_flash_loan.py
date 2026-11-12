"""Balancer V2 Flash Loan Adapter."""

from __future__ import annotations

from typing import Any, Dict

from liquidator.core.interfaces import FundingAdapter
from liquidator.core.models import ChainType, Token


class BalancerFlashLoanAdapter(FundingAdapter):
    """
    Balancer V2 Vault flash loan provider.
    Fee on Base is 0 bps (0%).
    """

    def __init__(self, vault_address: str = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"):
        self.vault_address = vault_address

    @property
    def name(self) -> str:
        return "balancer_v2_vault"

    @property
    def chain(self) -> ChainType:
        return ChainType.EVM

    async def get_available_liquidity(self, token: Token) -> int:
        # Returns maximum available flash loan liquidity (virtually unlimited for top assets on Base)
        return int(10_000_000 * (10 ** token.decimals))

    def calculate_fee(self, token: Token, amount_raw: int) -> int:
        # 0 bps on Base
        return 0

    def build_flash_loan_call(
        self,
        token: Token,
        amount_raw: int,
        receiver_address: str,
        callback_calldata: bytes,
    ) -> Dict[str, Any]:
        return {
            "target": self.vault_address,
            "method": "flashLoan",
            "params": [
                receiver_address,
                [token.address],
                [amount_raw],
                callback_calldata,
            ],
        }
