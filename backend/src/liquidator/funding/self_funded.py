"""Self-Funded Execution Adapter for chains without Flash Loan providers."""

from __future__ import annotations

from typing import Any, Dict

from liquidator.core.interfaces import FundingAdapter
from liquidator.core.models import ChainType, Token


class SelfFundedAdapter(FundingAdapter):
    """
    Self-funded balance provider for newly forked / long-tail EVM chains.
    Bypasses flash loan requirements, meaning:
    1. Zero flash loan fee (0 bps).
    2. Zero external flash loan contract dependencies.
    3. Low-competition advantage over flash-loan-only bots.
    """

    def __init__(
        self,
        wallet_address: str = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
        max_capital_usd: float = 10_000.0,
    ):
        self.wallet_address = wallet_address
        self.max_capital_usd = max_capital_usd

    @property
    def name(self) -> str:
        return "self_funded_wallet"

    @property
    def chain(self) -> ChainType:
        return ChainType.EVM

    async def get_available_liquidity(self, token: Token) -> int:
        """Returns maximum capital available for liquidation in token base units."""
        token_price = float(token.price_usd) if token.price_usd > 0 else 1.0
        tokens_available = self.max_capital_usd / token_price
        return int(tokens_available * (10 ** token.decimals))

    def calculate_fee(self, token: Token, amount_raw: int) -> int:
        """0 bps fee for self-funded balance."""
        return 0

    def build_flash_loan_call(
        self,
        token: Token,
        amount_raw: int,
        receiver_address: str,
        callback_calldata: bytes,
    ) -> Dict[str, Any]:
        """Constructs direct repayment call payload from wallet balance."""
        return {
            "target": receiver_address,
            "method": "directExecute",
            "params": [
                token.address,
                amount_raw,
                callback_calldata,
            ],
            "funding_mode": "SELF_FUNDED",
        }
