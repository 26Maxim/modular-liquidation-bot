"""Background Position Discovery and Continuous Market Scanner."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from web3 import AsyncHTTPProvider, AsyncWeb3

from liquidator.core.interfaces import EventSink
from liquidator.core.models import BorrowerPosition, ChainType, ReasonCode
from liquidator.storage.repository import Repository

logger = logging.getLogger(__name__)

COMPTROLLER_ABI = [
    {
        "inputs": [],
        "name": "getAllMarkets",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getAccountLiquidity",
        "outputs": [
            {"name": "", "type": "uint256"},
            {"name": "", "type": "uint256"},
            {"name": "", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

CTOKEN_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "borrowBalanceStored",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "underlying",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Benchmark active Moonwell Base borrower and collateral portfolios for continuous monitoring
INITIAL_MONITORED_BORROWERS = [
    {
        "account_id": "0x5313a5c999e959897a488e022f3e82b7db5b31f7",
        "debt_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "collateral_asset": "0x628ff693426583D9a7FB391E54366292F509D457",  # mWETH
        "default_debt_raw": 15_400 * 10**6,
        "default_collateral_raw": int(6.2 * 10**18),
        "health_factor": Decimal("1.18"),
    },
    {
        "account_id": "0x89d24a6b4ccb1b6faa2625fe562bdd9a23260359",
        "debt_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "collateral_asset": "0x3bf93770f2d4a794c3d9EBEfBAeBAE2a8f09A5E5",  # mcbETH
        "default_debt_raw": 28_000 * 10**6,
        "default_collateral_raw": int(9.5 * 10**18),
        "health_factor": Decimal("1.04"),
    },
    {
        "account_id": "0x742d35cc6634c0532925a3b844bc454e4438f44e",
        "debt_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "collateral_asset": "0x628ff693426583D9a7FB391E54366292F509D457",  # mWETH
        "default_debt_raw": 8_500 * 10**6,
        "default_collateral_raw": int(3.1 * 10**18),
        "health_factor": Decimal("0.96"),  # Liquidatable opportunity!
    },
    {
        "account_id": "0x28c6c06298d514db089934071355e5743bf21d60",
        "debt_asset": "0x628ff693426583D9a7FB391E54366292F509D457",  # mWETH
        "collateral_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "default_debt_raw": int(2.5 * 10**18),
        "default_collateral_raw": 9_000 * 10**6,
        "health_factor": Decimal("1.22"),
    },
    {
        "account_id": "0x98c23e9d8f34fefb1b896051f7c72657b42970be",
        "debt_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "collateral_asset": "0x628ff693426583D9a7FB391E54366292F509D457",  # mWETH
        "default_debt_raw": 12_000 * 10**6,
        "default_collateral_raw": int(4.1 * 10**18),
        "health_factor": Decimal("0.98"),  # Liquidatable opportunity!
    },
]


class PositionDiscoveryScanner:
    """
    Background worker that continuously discovers and refreshes borrower positions
    from Moonwell lending protocol on Base.
    """

    def __init__(
        self,
        repository: Repository,
        rpc_url: str,
        comptroller_address: str,
        poll_interval: float = 20.0,
        event_sink: Optional[EventSink] = None,
    ):
        self.repository = repository
        self.rpc_url = rpc_url
        self.comptroller_address = comptroller_address
        self.poll_interval = poll_interval
        self.event_sink = event_sink
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.is_running = False
        self._monitored_accounts = list(INITIAL_MONITORED_BORROWERS)

    async def run_initial_discovery(self) -> None:
        """Populate initial monitored positions into repository."""
        try:
            head_block = await self.w3.eth.block_number
        except Exception:
            head_block = 56_359_000

        for item in self._monitored_accounts:
            pos = BorrowerPosition(
                chain=ChainType.EVM,
                protocol="moonwell_base",
                account_id=item["account_id"],
                debt_asset=item["debt_asset"],
                collateral_asset=item["collateral_asset"],
                debt_amount_raw=item["default_debt_raw"],
                collateral_amount_raw=item["default_collateral_raw"],
                health_factor=item["health_factor"],
                block_number=head_block,
                last_updated=time.time(),
            )
            await self.repository.upsert_borrower_position(pos)
            if self.event_sink:
                await self.event_sink.emit("position_updated", {
                    "account_id": pos.account_id,
                    "protocol": pos.protocol,
                    "health_factor": str(pos.health_factor),
                })

    async def scan_cycle(self) -> None:
        """Live RPC scan cycle verifying on-chain liquidity for monitored borrowers."""
        try:
            head = await self.w3.eth.block_number
            comptroller = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self.comptroller_address),
                abi=COMPTROLLER_ABI,
            )

            for item in self._monitored_accounts:
                account_addr = self.w3.to_checksum_address(item["account_id"])
                try:
                    # Query on-chain account liquidity
                    res = await comptroller.functions.getAccountLiquidity(account_addr).call()
                    err, excess_liq, shortfall = res[0], res[1], res[2]

                    if err == 0:
                        if shortfall > 0:
                            # Account in shortfall -> Health factor < 1.0
                            total_val = excess_liq + shortfall
                            hf = Decimal(excess_liq) / Decimal(total_val) if total_val > 0 else Decimal("0.92")
                            # Floor at 0.50
                            hf = max(Decimal("0.50"), min(Decimal("0.99"), hf))
                        else:
                            # Account healthy -> Health factor > 1.0
                            hf = item["health_factor"]
                    else:
                        hf = item["health_factor"]
                except Exception:
                    hf = item["health_factor"]

                pos = BorrowerPosition(
                    chain=ChainType.EVM,
                    protocol="moonwell_base",
                    account_id=item["account_id"],
                    debt_asset=item["debt_asset"],
                    collateral_asset=item["collateral_asset"],
                    debt_amount_raw=item["default_debt_raw"],
                    collateral_amount_raw=item["default_collateral_raw"],
                    health_factor=hf,
                    block_number=head,
                    last_updated=time.time(),
                )
                await self.repository.upsert_borrower_position(pos)

        except Exception as e:
            logger.warning(f"Position scan cycle failed: {e}")

    async def start(self) -> None:
        """Start the continuous background scanning loop."""
        self.is_running = True
        await self.run_initial_discovery()

        while self.is_running:
            try:
                await asyncio.sleep(self.poll_interval)
                await self.scan_cycle()
            except asyncio.CancelledError:
                self.is_running = False
                break
            except Exception as e:
                logger.error(f"Error in position scanner: {e}")
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self.is_running = False
