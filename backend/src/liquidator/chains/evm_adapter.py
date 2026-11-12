"""EVM Chain Adapter implementation for Base and other EVM networks."""

from __future__ import annotations

import time
from typing import Any, Dict, Tuple

from liquidator.core.interfaces import ChainAdapter
from liquidator.core.models import (
    AdapterStatus,
    AttemptOutcome,
    ChainType,
    ReasonCode,
    SimulationResult,
)
from web3 import AsyncHTTPProvider, AsyncWeb3


class EVMChainAdapter(ChainAdapter):
    """
    EVM adapter using Web3.py.
    Provides RPC queries, static call simulation, and transaction submission.
    """

    def __init__(
        self,
        rpc_url: str,
        chain_id: int = 8453,  # Base Mainnet
        status_override: AdapterStatus = AdapterStatus.EXECUTION_IMPLEMENTED,
    ):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self._status = status_override
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self._last_observed_block: int = 0
        self._last_block_time: float = 0.0

    @property
    def chain_type(self) -> ChainType:
        return ChainType.EVM

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def get_latest_block_number(self) -> int:
        try:
            block = await self.w3.eth.block_number
            self._last_observed_block = block
            self._last_block_time = time.time()
            return block
        except Exception:
            if self._last_observed_block > 0:
                return self._last_observed_block
            return 0

    async def get_native_balance(self, address: str) -> int:
        try:
            checksummed = self.w3.to_checksum_address(address)
            balance = await self.w3.eth.get_balance(checksummed)
            return balance
        except Exception:
            return 0

    async def estimate_gas(self, tx_params: Dict[str, Any]) -> int:
        try:
            gas = await self.w3.eth.estimate_gas(tx_params)
            return int(gas * 1.2)  # Add 20% safety buffer
        except Exception:
            return 350_000  # Conservative fallback for complex flash-liquidation

    async def simulate_transaction(self, tx_params: Dict[str, Any]) -> SimulationResult:
        """
        Simulate transaction execution using eth_call.
        Returns detailed gas and failure diagnostics.
        """
        try:
            res = await self.w3.eth.call(tx_params)
            gas_est = await self.estimate_gas(tx_params)
            return SimulationResult(
                success=True,
                gas_used=gas_est,
                return_data=res.hex() if hasattr(res, "hex") else str(res),
            )
        except Exception as e:
            err_msg = str(e)
            rejection_code = ReasonCode.SIMULATION_REVERTED
            if "InsufficientProfit" in err_msg or "PROFIT_FLOOR_FAILED" in err_msg:
                rejection_code = ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE
            elif "SLIPPAGE" in err_msg:
                rejection_code = ReasonCode.SLIPPAGE_EXCEEDED
            elif "HEALTH_FACTOR" in err_msg:
                rejection_code = ReasonCode.HEALTH_FACTOR_HEALTHY

            return SimulationResult(
                success=False,
                error_message=err_msg,
                rejection_code=rejection_code,
            )

    async def submit_transaction(self, signed_tx_payload: Any) -> str:
        """Broadcast raw signed transaction payload."""
        try:
            tx_hash_bytes = await self.w3.eth.send_raw_transaction(signed_tx_payload)
            return tx_hash_bytes.hex()
        except Exception as e:
            raise RuntimeError(f"EVM transaction broadcast failed: {e}")

    async def get_transaction_status(self, tx_hash: str) -> Tuple[AttemptOutcome, Dict[str, Any]]:
        """Query transaction receipt and finality."""
        try:
            receipt = await self.w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None:
                return AttemptOutcome.PENDING, {}

            status = receipt.get("status")
            gas_used = receipt.get("gasUsed", 0)
            block_number = receipt.get("blockNumber", 0)

            outcome = AttemptOutcome.SUCCESS if status == 1 else AttemptOutcome.REVERTED
            return outcome, {
                "gas_used": gas_used,
                "block_number": block_number,
                "logs_count": len(receipt.get("logs", [])),
            }
        except Exception as e:
            return AttemptOutcome.PENDING, {"error": str(e)}
