"""Aptos Chain Adapter (Researched Scaffolding)."""

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


class AptosChainAdapter(ChainAdapter):
    """
    Aptos network adapter communicating via Aptos Node REST API v1.
    Status: RESEARCHED (Interfaces and transaction serialization documented).
    """

    def __init__(self, rpc_url: str = "https://fullnode.mainnet.aptoslabs.com/v1"):
        self.rpc_url = rpc_url.rstrip("/")
        self._status = AdapterStatus.RESEARCHED
        self._last_ledger_version: int = 0

    @property
    def chain_type(self) -> ChainType:
        return ChainType.APTOS

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def get_latest_block_number(self) -> int:
        """Fetch latest ledger version / block height."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{self.rpc_url}")
                data = res.json()
                version = int(data.get("ledger_version", self._last_ledger_version))
                self._last_ledger_version = version
                return self._last_ledger_version
        except Exception:
            return self._last_ledger_version

    async def get_native_balance(self, address: str) -> int:
        """Fetch APT coin balance in octas (1 APT = 1e8 octas)."""
        try:
            resource_type = "0x1::coin::CoinStore<0x1::aptos_coin::AptosCoin>"
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{self.rpc_url}/accounts/{address}/resource/{resource_type}")
                data = res.json()
                return int(data.get("data", {}).get("coin", {}).get("value", 0))
        except Exception:
            return 0

    async def estimate_gas(self, tx_params: Dict[str, Any]) -> int:
        # Standard Aptos Move transaction gas units
        return 2_000  # max gas amount

    async def simulate_transaction(self, tx_params: Dict[str, Any]) -> SimulationResult:
        """Simulate transaction via Aptos /transactions/simulate endpoint."""
        payload = tx_params.get("payload")
        if not payload:
            return SimulationResult(
                success=False,
                error_message="Missing Aptos transaction payload",
                rejection_code=ReasonCode.SIMULATION_REVERTED,
            )

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(f"{self.rpc_url}/transactions/simulate", json=payload)
                data = res.json()
                if isinstance(data, list) and len(data) > 0:
                    sim_item = data[0]
                    success = sim_item.get("success", False)
                    gas_used = int(sim_item.get("gas_used", 0))
                    vm_status = sim_item.get("vm_status", "")
                    if not success:
                        return SimulationResult(
                            success=False,
                            gas_used=gas_used,
                            error_message=vm_status,
                            rejection_code=ReasonCode.SIMULATION_REVERTED,
                        )
                    return SimulationResult(success=True, gas_used=gas_used)
                return SimulationResult(
                    success=False,
                    error_message=str(data),
                    rejection_code=ReasonCode.SIMULATION_REVERTED,
                )
        except Exception as e:
            return SimulationResult(
                success=False,
                error_message=str(e),
                rejection_code=ReasonCode.SIMULATION_REVERTED,
            )

    async def submit_transaction(self, signed_tx_payload: Any) -> str:
        raise NotImplementedError("Aptos live execution is not enabled. Status: RESEARCHED.")

    async def get_transaction_status(self, tx_hash: str) -> Tuple[AttemptOutcome, Dict[str, Any]]:
        return AttemptOutcome.PENDING, {"status": "researched"}
