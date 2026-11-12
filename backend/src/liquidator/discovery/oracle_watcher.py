"""Event-driven Oracle Update Watcher."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional

from liquidator.core.models import ChainType, OraclePriceUpdate


class OracleUpdateWatcher:
    """
    Listens for price update events from on-chain feeds (Chainlink Aggregators, Pyth).
    Triggers prioritized re-evaluation of monitored positions holding the updated asset.
    Monitors its own health, reconnection count, and staleness lag.
    """

    def __init__(
        self,
        chain: ChainType,
        max_staleness_seconds: int = 300,
        on_price_update: Optional[Callable[[OraclePriceUpdate], Any]] = None,
    ):
        self.chain = chain
        self.max_staleness_seconds = max_staleness_seconds
        self.on_price_update = on_price_update

        # Health tracking
        self.last_update_received: float = time.time()
        self.reconnection_count: int = 0
        self.total_updates_received: int = 0
        self.is_running: bool = False
        self.watched_assets: Dict[str, str] = {}  # symbol -> address
        self._latest_prices: Dict[str, OraclePriceUpdate] = {}
        self._alert_callback: Optional[Callable[[str], Any]] = None

    def register_asset(self, symbol: str, address: str) -> None:
        self.watched_assets[symbol.upper()] = address.lower()

    def set_alert_callback(self, cb: Callable[[str], Any]) -> None:
        self._alert_callback = cb

    @property
    def lag_seconds(self) -> float:
        return time.time() - self.last_update_received

    @property
    def is_stale(self) -> bool:
        return self.lag_seconds > self.max_staleness_seconds

    def get_health_status(self) -> Dict[str, Any]:
        return {
            "chain": self.chain.value,
            "running": self.is_running,
            "last_update_received": self.last_update_received,
            "lag_seconds": round(self.lag_seconds, 2),
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

    async def start_polling_loop(self, poll_interval: float = 15.0) -> None:
        """Background health check and recovery loop."""
        self.is_running = True
        try:
            while self.is_running:
                await asyncio.sleep(poll_interval)
                if self.is_stale:
                    msg = (
                        f"WARNING: Oracle watcher on {self.chain.value} is stale! "
                        f"Lag is {self.lag_seconds:.1f}s (threshold: {self.max_staleness_seconds}s)"
                    )
                    if self._alert_callback:
                        self._alert_callback(msg)
        except asyncio.CancelledError:
            self.is_running = False

    def stop(self) -> None:
        self.is_running = False
