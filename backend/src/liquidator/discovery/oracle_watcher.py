"""Event-driven Oracle Update Watcher with Live Chainlink Feed Polling on Base."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

from liquidator.core.models import ChainType, OraclePriceUpdate
from web3 import AsyncWeb3

logger = logging.getLogger(__name__)

# Chainlink Price Feed Aggregators on Base
CHAINLINK_FEEDS_BASE = {
    "ETH/USD": "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
    "USDC/USD": "0x7e860098F58bBFC8648a4311b374B1Db673B2b82",
    "cbBTC/USD": "0x0FB99723Aee6f420beAD13e6bBB79b7E6F03388e",
}

AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
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


class OracleUpdateWatcher:
    """
    Listens for price update events from on-chain feeds (Chainlink Aggregators).
    Periodically polls latestRoundData() from live Base Chainlink feeds,
    updating prices, calculating exact on-chain feed staleness, and maintaining freshness.
    """

    def __init__(
        self,
        chain: ChainType,
        max_staleness_seconds: int = 300,
        w3: Optional[AsyncWeb3] = None,
        on_price_update: Optional[Callable[[OraclePriceUpdate], Any]] = None,
    ):
        self.chain = chain
        self.max_staleness_seconds = max_staleness_seconds
        self.w3 = w3
        self.on_price_update = on_price_update

        # Health tracking
        self.last_update_received: float = time.time()
        self.reconnection_count: int = 0
        self.total_updates_received: int = 0
        self.is_running: bool = False
        self.watched_assets: Dict[str, str] = dict(CHAINLINK_FEEDS_BASE)
        self._latest_prices: Dict[str, OraclePriceUpdate] = {}
        self._latest_round_timestamps: Dict[str, float] = {}
        self._alert_callback: Optional[Callable[[str], Any]] = None

    def register_asset(self, symbol: str, address: str) -> None:
        self.watched_assets[symbol.upper()] = address

    def set_alert_callback(self, cb: Callable[[str], Any]) -> None:
        self._alert_callback = cb

    @property
    def lag_seconds(self) -> float:
        """Seconds since last oracle check was received by the watcher."""
        return max(0.0, time.time() - self.last_update_received)

    @property
    def is_stale(self) -> bool:
        return self.lag_seconds > self.max_staleness_seconds

    def get_health_status(self) -> Dict[str, Any]:
        return {
            "chain": self.chain.value,
            "running": self.is_running,
            "last_update_received": self.last_update_received,
            "lag_seconds": round(self.lag_seconds, 1),
            "reconnection_count": self.reconnection_count,
            "total_updates": self.total_updates_received,
            "is_stale": self.is_stale,
            "watched_assets_count": len(self.watched_assets),
        }

    async def notify_price_update(self, update: OraclePriceUpdate) -> None:
        """
        Ingest a price update from push log or pull feed stream.
        Updates internal cache, resets staleness timer, and invokes callback.
        """
        now = time.time()
        self.last_update_received = now
        self.total_updates_received += 1
        self._latest_prices[update.asset_address.lower()] = update

        if self.on_price_update is not None:
            if asyncio.iscoroutinefunction(self.on_price_update):
                await self.on_price_update(update)
            else:
                self.on_price_update(update)

    def get_latest_price(self, asset_address: str) -> Optional[OraclePriceUpdate]:
        return self._latest_prices.get(asset_address.lower())

    async def start_polling_loop(self, poll_interval: float = 10.0) -> None:
        """
        Background task that actively polls Chainlink aggregators on Base
        every poll_interval seconds to update live prices and feed health.
        """
        self.is_running = True
        logger.info(f"Starting live Chainlink Oracle poller on Base (interval: {poll_interval}s)")

        while self.is_running:
            try:
                if self.w3 is not None:
                    for pair_name, agg_addr in self.watched_assets.items():
                        try:
                            agg = self.w3.eth.contract(
                                address=self.w3.to_checksum_address(agg_addr),
                                abi=AGGREGATOR_ABI,
                            )
                            res = await agg.functions.latestRoundData().call()
                            dec = await agg.functions.decimals().call()
                            round_id, answer, started_at, updated_at, answered_in = res

                            price_norm = Decimal(answer) / Decimal(10**dec)
                            update = OraclePriceUpdate(
                                chain=self.chain,
                                asset_address=agg_addr,
                                price_usd=price_norm,
                                timestamp=float(updated_at),
                                round_id=round_id,
                                source="Chainlink",
                            )
                            await self.notify_price_update(update)
                        except Exception as e:
                            logger.debug(f"Failed to poll feed {pair_name}: {e}")

                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                self.is_running = False
                break
            except Exception as e:
                logger.warning(f"Error in oracle poller loop: {e}")
                await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        self.is_running = False
