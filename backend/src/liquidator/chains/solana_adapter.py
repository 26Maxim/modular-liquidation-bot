"""Solana Chain Adapter (Read-Only / Simulation Scaffolding)."""

from __future__ import annotations

from typing import Any, Dict, Tuple

import httpx
from liquidator.core.interfaces import ChainAdapter
from liquidator.core.models import (
    AdapterStatus,
    AttemptOutcome,
    ChainType,
    ReasonCode,
    SimulationResult,
)


class SolanaChainAdapter(ChainAdapter):
    """
    Solana chain adapter communicating via standard Solana JSON-RPC.
    Status: READ_ONLY (Execution not claimed until verified on-chain program deployed).
    """

    def __init__(self, rpc_url: str = "https://api.mainnet-beta.solana.com"):
        self.rpc_url = rpc_url
        self._status = AdapterStatus.READ_ONLY
        self._last_slot: int = 0

    @property
    def chain_type(self) -> ChainType:
        return ChainType.SOLANA

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def get_latest_block_number(self) -> int:
        """Fetch current slot on Solana."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
                )
                data = res.json()
                self._last_slot = data.get("result", self._last_slot)
                return self._last_slot
        except Exception:
            return self._last_slot

    async def get_native_balance(self, address: str) -> int:
        """Fetch SOL balance in lamports."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(
                    self.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getBalance",
                        "params": [address],
                    },
                )
                data = res.json()
                return data.get("result", {}).get("value", 0)
        except Exception:
            return 0

    async def estimate_gas(self, tx_params: Dict[str, Any]) -> int:
        # Base signature fee (5000 lamports) + default compute priority fee (10,000 lamports)
        return 15_000

    async def simulate_transaction(self, tx_params: Dict[str, Any]) -> SimulationResult:
        """
        Simulates via Solana simulateTransaction RPC endpoint.
        Verifies account states, compute units consumed, and program logs.
        """
        b64_tx = tx_params.get("b64_tx")
        if not b64_tx:
            return SimulationResult(
                success=False,
                error_message="Missing encoded Solana transaction",
                rejection_code=ReasonCode.SIMULATION_REVERTED,
            )

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(
                    self.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "simulateTransaction",
                        "params": [
                            b64_tx,
                            {"sigVerify": False, "encoding": "base64"},
                        ],
                    },
                )
                data = res.json()
                result_val = data.get("result", {}).get("value", {})
                err = result_val.get("err")
                units_consumed = result_val.get("unitsConsumed", 0)
                logs = result_val.get("logs", [])

                if err is not None:
                    return SimulationResult(
                        success=False,
                        gas_used=units_consumed,
                        error_message=str(err),
                        rejection_code=ReasonCode.SIMULATION_REVERTED,
                        logs=logs,
                    )
                return SimulationResult(
                    success=True,
                    gas_used=units_consumed,
                    logs=logs,
                )
        except Exception as e:
            return SimulationResult(
                success=False,
                error_message=str(e),
                rejection_code=ReasonCode.SIMULATION_REVERTED,
            )

    async def submit_transaction(self, signed_tx_payload: Any) -> str:
        raise NotImplementedError(
            "Solana live execution is not enabled. Status: READ_ONLY. "
            "Requires custom on-chain liquidator program deployment."
        )

    async def get_transaction_status(self, tx_hash: str) -> Tuple[AttemptOutcome, Dict[str, Any]]:
        return AttemptOutcome.PENDING, {"status": "read_only"}
