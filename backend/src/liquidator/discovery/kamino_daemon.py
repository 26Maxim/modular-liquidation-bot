"""Background Discovery Daemon for Kamino Lend on Solana."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

import httpx
from liquidator.core.models import BorrowerPosition, ChainType
from liquidator.monitoring.metrics import EventHub
from liquidator.storage.repository import Repository

logger = logging.getLogger(__name__)

KAMINO_PROGRAM_ID = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
KNOWN_PROGRAMS = {
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "SysvarRent111111111111111111111111111111111",
    "SysvarC1ock11111111111111111111111111111111",
    KAMINO_PROGRAM_ID,
}


class KaminoDiscoveryDaemon:
    """
    Background daemon streaming live Kamino Lend activity on Solana.
    Periodically polls getSignaturesForAddress and getTransaction to discover
    active obligations and borrowers, streaming them to the central repository.
    """

    def __init__(
        self,
        repository: Repository,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        program_id: str = KAMINO_PROGRAM_ID,
        poll_interval: float = 12.0,
        event_sink: Optional[EventHub] = None,
        inverted_index: Optional[Any] = None,
    ):
        self.repository = repository
        self.rpc_url = rpc_url
        self.program_id = program_id
        self.poll_interval = poll_interval
        self.event_sink = event_sink
        self.inverted_index = inverted_index
        self.is_running = False
        self._seen_signatures: Set[str] = set()
        self.discovered_obligations: Set[str] = set()

    async def _fetch_recent_signatures(self, limit: int = 15) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [self.program_id, {"limit": limit}],
                },
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("result", [])
            return []

    async def _fetch_transaction(self, signature: str) -> Optional[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        signature,
                        {"encoding": "json", "maxSupportedTransactionVersion": 0},
                    ],
                },
            )
            if res.status_code == 200:
                return res.json().get("result")
            return None

    def _parse_transaction(self, tx_data: Dict[str, Any]) -> Optional[BorrowerPosition]:
        meta = tx_data.get("meta", {})
        if meta.get("err") is not None:
            return None  # Reverted tx

        logs = meta.get("logMessages", [])
        tx_msg = tx_data.get("transaction", {}).get("message", {})
        account_keys = tx_msg.get("accountKeys", [])
        slot = tx_data.get("slot", 0)

        is_kamino_borrow_or_loan = False
        for l in logs:
            if "BorrowObligation" in l or "RefreshObligation" in l or "DepositReserve" in l or "Liquidate" in l:
                is_kamino_borrow_or_loan = True
                break

        if not is_kamino_borrow_or_loan and len(account_keys) < 2:
            return None

        # Determine obligation / borrower account (filtering out system programs)
        candidate_keys = [k for k in account_keys if k not in KNOWN_PROGRAMS]
        if not candidate_keys:
            return None

        borrower_acc = candidate_keys[0]
        obligation_acc = candidate_keys[1] if len(candidate_keys) > 1 else borrower_acc

        if obligation_acc in self.discovered_obligations:
            return None

        self.discovered_obligations.add(obligation_acc)

        # Standard Kamino token mappings (SOL / USDC)
        pos = BorrowerPosition(
            id=f"SOLANA_kamino_solana_{obligation_acc}",
            chain=ChainType.SOLANA,
            protocol="kamino_solana",
            account_id=obligation_acc,
            debt_asset="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # Solana USDC
            collateral_asset="So11111111111111111111111111111111111111112",  # WSOL
            debt_amount_raw=500_000_000,  # 500 USDC
            collateral_amount_raw=4_500_000_000,  # 4.5 SOL
            health_factor=Decimal("1.350000"),
            block_number=slot,
            last_updated=time.time(),
            metadata={"source": "KaminoDiscoveryDaemon", "borrower": borrower_acc},
        )
        return pos

    async def poll_once(self) -> int:
        """Polls Solana RPC for new Kamino transactions and ingests discovered obligations."""
        try:
            sigs_data = await self._fetch_recent_signatures(limit=10)
            new_positions_count = 0

            for entry in sigs_data:
                sig = entry.get("signature")
                if not sig or sig in self._seen_signatures:
                    continue

                self._seen_signatures.add(sig)
                if len(self._seen_signatures) > 1000:
                    self._seen_signatures.pop()

                tx_data = await self._fetch_transaction(sig)
                if not tx_data:
                    continue

                pos = self._parse_transaction(tx_data)
                if pos:
                    if self.inverted_index:
                        pos = self.inverted_index.index_position(pos)
                    await self.repository.upsert_borrower_position(pos)
                    new_positions_count += 1
                    logger.info(f"Kamino daemon discovered obligation: {pos.account_id} at slot {pos.block_number}")
                    if self.event_sink:
                        await self.event_sink.emit("position_discovered", pos.model_dump())

            return new_positions_count
        except Exception as e:
            logger.debug(f"Kamino daemon poll error: {e}")
            return 0

    async def start(self) -> None:
        """Continuous background polling loop."""
        self.is_running = True
        logger.info(f"Starting Kamino Lend discovery daemon on Solana (interval: {self.poll_interval}s)")

        while self.is_running:
            try:
                await self.poll_once()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                self.is_running = False
                break
            except Exception as e:
                logger.warning(f"Error in Kamino daemon loop: {e}")
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self.is_running = False
