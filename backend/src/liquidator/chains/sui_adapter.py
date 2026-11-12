"""Sui Chain Adapter (Read-Only / Simulation Scaffolding)."""

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


class SuiChainAdapter(ChainAdapter):
    """
    Sui network adapter communicating via Sui JSON-RPC 2.0.
    Status: READ_ONLY.
    """

    def __init__(self, rpc_url: str = "https://fullnode.mainnet.sui.io:443"):
        self.rpc_url = rpc_url
        self._status = AdapterStatus.READ_ONLY
        self._last_checkpoint: int = 0

    @property
    def chain_type(self) -> ChainType:
        return ChainType.SUI

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def get_latest_block_number(self) -> int:
        """Fetch latest checkpoint sequence number on Sui."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(
                    self.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "sui_getLatestCheckpointSequenceNumber",
                        "params": [],
                    },
                )
                data = res.json()
                seq = int(data.get("result", self._last_checkpoint))
                self._last_checkpoint = seq
                return self._last_checkpoint
        except Exception:
            return self._last_checkpoint

    async def get_native_balance(self, address: str) -> int:
        """Fetch total SUI coin balance in MIST (1 SUI = 1e9 MIST)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(
                    self.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "suix_getBalance",
                        "params": [address, "0x2::sui::SUI"],
                    },
                )
                data = res.json()
                return int(data.get("result", {}).get("totalBalance", 0))
        except Exception:
            return 0

    async def estimate_gas(self, tx_params: Dict[str, Any]) -> int:
        # Standard Sui PTB budget in MIST
        return 5_000_000  # ~0.005 SUI

    async def simulate_transaction(self, tx_params: Dict[str, Any]) -> SimulationResult:
        """Simulate Sui Programmable Transaction Block using sui_devInspectTransactionBlock."""
        tx_bytes_b64 = tx_params.get("tx_bytes")
        sender = tx_params.get("sender", "0x0000000000000000000000000000000000000000000000000000000000000000")
        if not tx_bytes_b64:
            return SimulationResult(
                success=False,
                error_message="Missing transaction bytes for Sui devInspect",
                rejection_code=ReasonCode.SIMULATION_REVERTED,
            )

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(
                    self.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "sui_devInspectTransactionBlock",
                        "params": [sender, tx_bytes_b64],
                    },
                )
                data = res.json()
                effects = data.get("result", {}).get("effects", {})
                status_info = effects.get("status", {})
                is_success = status_info.get("status") == "success"

                gas_used = 0
                gas_summary = effects.get("gasUsed", {})
                if gas_summary:
                    comp_cost = int(gas_summary.get("computationCost", 0))
                    stor_cost = int(gas_summary.get("storageCost", 0))
                    gas_used = comp_cost + stor_cost

                if not is_success:
                    err_msg = status_info.get("error", "Execution failed")
                    return SimulationResult(
                        success=False,
                        gas_used=gas_used,
                        error_message=err_msg,
                        rejection_code=ReasonCode.SIMULATION_REVERTED,
                    )

                return SimulationResult(
                    success=True,
                    gas_used=gas_used,
                )
        except Exception as e:
            return SimulationResult(
                success=False,
                error_message=str(e),
                rejection_code=ReasonCode.SIMULATION_REVERTED,
            )

    async def submit_transaction(self, signed_tx_payload: Any) -> str:
        raise NotImplementedError("Sui live execution is not enabled. Status: READ_ONLY.")

    async def get_transaction_status(self, tx_hash: str) -> Tuple[AttemptOutcome, Dict[str, Any]]:
        return AttemptOutcome.PENDING, {"status": "read_only"}
