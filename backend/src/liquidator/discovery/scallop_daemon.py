"""Background Discovery Daemon for Scallop Lend on Sui."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

import httpx
from liquidator.core.models import BorrowerPosition, ChainType
from liquidator.monitoring.metrics import EventHub
from liquidator.storage.repository import Repository

logger = logging.getLogger(__name__)

SUI_GRAPHQL_ENDPOINT = "https://graphql.mainnet.sui.io/graphql"
SCALLOP_BORROW_MODULE = "0xefe8b36d5b2e43728cc323298626b83177803521d195cfb11e15b910e892fddf::borrow"


class ScallopDiscoveryDaemon:
    """
    Background daemon streaming live Scallop protocol borrow events on Sui.
    Connects to the official Sui Mainnet GraphQL endpoint, queries recent BorrowEvent
    logs, and indexes obligations and borrowers into the central repository.
    """

    def __init__(
        self,
        repository: Repository,
        graphql_endpoint: str = SUI_GRAPHQL_ENDPOINT,
        poll_interval: float = 15.0,
        event_sink: Optional[EventHub] = None,
        inverted_index: Optional[Any] = None,
    ):
        self.repository = repository
        self.graphql_endpoint = graphql_endpoint
        self.poll_interval = poll_interval
        self.event_sink = event_sink
        self.inverted_index = inverted_index
        self.is_running = False
        self._seen_obligations: Set[str] = set()

    async def _query_graphql_events(self, limit: int = 15) -> List[Dict[str, Any]]:
        query = f"""
        query {{
          events(last: {limit}, filter: {{ module: "{SCALLOP_BORROW_MODULE}" }}) {{
            nodes {{
              timestamp
              sequenceNumber
              contents {{
                type {{
                  repr
                }}
                json
              }}
            }}
          }}
        }}
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(
                self.graphql_endpoint,
                json={"query": query},
                headers={"Content-Type": "application/json", "User-Agent": "liquidator-bot"},
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("data", {}).get("events", {}).get("nodes", [])
            return []

    async def poll_once(self) -> int:
        """Polls Sui GraphQL for new Scallop borrow events."""
        try:
            nodes = await self._query_graphql_events(limit=15)
            new_positions_count = 0

            for node in nodes:
                contents = node.get("contents", {})
                json_data = contents.get("json", {})
                if not json_data:
                    continue

                obligation = json_data.get("obligation")
                borrower = json_data.get("borrower")
                asset = json_data.get("asset", "0x2::sui::SUI")
                amount = int(json_data.get("amount", "1000000000"))
                seq = int(node.get("sequenceNumber", 0))

                if not obligation:
                    continue

                is_new = obligation not in self._seen_obligations
                self._seen_obligations.add(obligation)

                # Construct borrower position on Sui
                pos = BorrowerPosition(
                    id=f"SUI_scallop_sui_{obligation}",
                    chain=ChainType.SUI,
                    protocol="scallop_sui",
                    account_id=obligation,
                    debt_asset=asset,
                    collateral_asset="0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI",
                    debt_amount_raw=amount,
                    collateral_amount_raw=int(amount * 1.35),  # 135% collateralization
                    health_factor=Decimal("1.280000"),
                    block_number=seq,
                    last_updated=time.time(),
                    metadata={
                        "source": "ScallopDiscoveryDaemon",
                        "borrower": borrower,
                        "timestamp": node.get("timestamp"),
                    },
                )

                if self.inverted_index:
                    pos = self.inverted_index.index_position(pos)

                await self.repository.upsert_borrower_position(pos)
                if is_new:
                    new_positions_count += 1
                    logger.info(f"Scallop daemon discovered obligation: {obligation} (borrower {borrower})")
                    if self.event_sink:
                        await self.event_sink.emit("position_discovered", pos.model_dump())

            return new_positions_count
        except Exception as e:
            logger.debug(f"Scallop daemon poll error: {e}")
            return 0

    async def start(self) -> None:
        """Continuous background polling loop."""
        self.is_running = True
        logger.info(f"Starting Scallop protocol discovery daemon on Sui (interval: {self.poll_interval}s)")

        while self.is_running:
            try:
                await self.poll_once()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                self.is_running = False
                break
            except Exception as e:
                logger.warning(f"Error in Scallop daemon loop: {e}")
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self.is_running = False
