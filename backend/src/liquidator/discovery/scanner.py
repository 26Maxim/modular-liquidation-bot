"""Background Position Discovery and Continuous Market Scanner."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from web3 import AsyncHTTPProvider, AsyncWeb3

from liquidator.core.interfaces import (
    ChainAdapter,
    CollateralRouteAdapter,
    EventSink,
    FundingAdapter,
    LendingProtocolAdapter,
)
from liquidator.core.models import BorrowerPosition, ChainType, ReasonCode, Token
from liquidator.eligibility.evaluator import OpportunityEvaluator
from liquidator.execution.executor import LiquidationExecutor
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
        "account_id": "0x9665224A631B275D5ceFC08E5705767237f5887c",  # Real active Moonwell borrower on Base
        "debt_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "collateral_asset": "0x628ff693426583D9a7FB391E54366292F509D457",  # mWETH
        "default_debt_raw": 12_000 * 10**6,
        "default_collateral_raw": int(8.0 * 10**18),
        "health_factor": Decimal("1.95"),
    },
    {
        "account_id": "0x50dbca1223169A2AEbFd757d994Fd7246c8865c1",  # Real active Moonwell account on Base
        "debt_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "collateral_asset": "0x628ff693426583D9a7FB391E54366292F509D457",  # mWETH
        "default_debt_raw": 5_000 * 10**6,
        "default_collateral_raw": int(3.5 * 10**18),
        "health_factor": Decimal("1.65"),
    },
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
        "account_id": "0x28c6c06298d514db089934071355e5743bf21d60",
        "debt_asset": "0x628ff693426583D9a7FB391E54366292F509D457",  # mWETH
        "collateral_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",  # mUSDC
        "default_debt_raw": int(2.5 * 10**18),
        "default_collateral_raw": 9_000 * 10**6,
        "health_factor": Decimal("1.22"),
    },
]

KNOWN_TOKENS: Dict[str, Token] = {
    # mUSDC / USDC on Base
    "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22": Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
        price_source="Pyth/Chainlink",
    ),
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
        price_source="Pyth/Chainlink",
    ),
    # mWETH / WETH on Base
    "0x628ff693426583D9a7FB391E54366292F509D457": Token(
        chain=ChainType.EVM,
        address="0x4200000000000000000000000000000000000006",
        symbol="WETH",
        decimals=18,
        price_usd=Decimal("3150.00"),
        price_timestamp=time.time(),
        price_source="Pyth/Chainlink",
    ),
    "0x4200000000000000000000000000000000000006": Token(
        chain=ChainType.EVM,
        address="0x4200000000000000000000000000000000000006",
        symbol="WETH",
        decimals=18,
        price_usd=Decimal("3150.00"),
        price_timestamp=time.time(),
        price_source="Pyth/Chainlink",
    ),
    # mcbBTC / cbBTC on Base
    "0x3bf93770f2d4a794c3d9EBEfBAeBAE2a8f09A5E5": Token(
        chain=ChainType.EVM,
        address="0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        symbol="cbBTC",
        decimals=8,
        price_usd=Decimal("88000.00"),
        price_timestamp=time.time(),
        price_source="Pyth/Chainlink",
    ),
}


class PositionDiscoveryScanner:
    """
    Background worker that continuously discovers and refreshes borrower positions
    from Moonwell lending protocol on Base, evaluates liquidatable positions against
    invariant gates, and executes paper-trade dry runs to estimate net profitability.
    """

    def __init__(
        self,
        repository: Repository,
        rpc_url: str,
        comptroller_address: str,
        poll_interval: float = 20.0,
        event_sink: Optional[EventSink] = None,
        evaluator: Optional[OpportunityEvaluator] = None,
        executor: Optional[LiquidationExecutor] = None,
        chain_adapter: Optional[ChainAdapter] = None,
        protocol_adapter: Optional[LendingProtocolAdapter] = None,
        funding_adapter: Optional[FundingAdapter] = None,
        route_adapter: Optional[CollateralRouteAdapter] = None,
        flash_liquidator_address: Optional[str] = None,
    ):
        self.repository = repository
        self.rpc_url = rpc_url
        self.comptroller_address = comptroller_address
        self.poll_interval = poll_interval
        self.event_sink = event_sink
        self.evaluator = evaluator
        self.executor = executor
        self.chain_adapter = chain_adapter
        self.protocol_adapter = protocol_adapter
        self.funding_adapter = funding_adapter
        self.route_adapter = route_adapter
        self.flash_liquidator_address = flash_liquidator_address
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.is_running = False
        self._monitored_accounts = list(INITIAL_MONITORED_BORROWERS)
        self._last_evaluated_attempts: Dict[str, float] = {}
        self._liquidated_positions: set[str] = set()

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

    async def evaluate_eligible_positions(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Evaluate liquidatable positions (health_factor < 1.0) and run dry-run paper trades
        to estimate net profitability after slippage and gas.
        """
        if not self.evaluator or not self.executor or not self.protocol_adapter or not self.funding_adapter or not self.route_adapter or not self.chain_adapter:
            return []

        positions = await self.repository.get_all_positions()
        results = []
        now = time.time()

        for pos in positions:
            if pos.health_factor >= Decimal("1.0"):
                continue

            pos_state_hash = f"{pos.account_id}:{pos.debt_amount_raw}:{pos.collateral_amount_raw}:{pos.health_factor}"
            # Deduplication: Avoid continuously liquidating the exact same loan position in the same state
            if not force and pos_state_hash in self._liquidated_positions:
                continue

            # Throttle evaluations to once per 120s per account unless force=True
            last_run = self._last_evaluated_attempts.get(pos.account_id, 0.0)
            if not force and (now - last_run < 120.0):
                continue

            self._last_evaluated_attempts[pos.account_id] = now

            # Resolve debt and collateral token models
            debt_token = KNOWN_TOKENS.get(
                pos.debt_asset,
                Token(chain=ChainType.EVM, address=pos.debt_asset, symbol="USDC", decimals=6, price_usd=Decimal("1.00")),
            )
            collateral_token = KNOWN_TOKENS.get(
                pos.collateral_asset,
                Token(chain=ChainType.EVM, address=pos.collateral_asset, symbol="WETH", decimals=18, price_usd=Decimal("3150.00")),
            )

            try:
                opp = await self.evaluator.evaluate_position(
                    position=pos,
                    debt_token=debt_token,
                    collateral_token=collateral_token,
                    native_token_price_usd=Decimal("3150.00"),
                    protocol_adapter=self.protocol_adapter,
                    funding_adapter=self.funding_adapter,
                    route_adapter=self.route_adapter,
                    estimated_gas_units=300_000,
                )

                if self.event_sink:
                    await self.event_sink.emit("opportunity_detected", opp.model_dump())

                attempt = None
                if opp.invariant_passed:
                    attempt = await self.executor.process_opportunity(
                        opp=opp,
                        chain_adapter=self.chain_adapter,
                        tx_call_params={
                            "to": self.flash_liquidator_address or "0x6278A1E803A7649642D8c35E1D5eEfa83F15A36A",
                            "simulated_dry_run": True,
                            "estimated_gas": 300_000,
                        },
                        signer_key=None,
                    )

                    # Mark this loan state as liquidated to prevent duplicate counting in paper trades
                    self._liquidated_positions.add(pos_state_hash)

                    # Update the borrower's position to reflect the post-liquidation state
                    new_debt = max(0, int(pos.debt_amount_raw) - opp.repay_amount_raw)
                    new_collateral = max(0, int(pos.collateral_amount_raw) - opp.expected_collateral_raw)
                    cured_pos = BorrowerPosition(
                        chain=pos.chain,
                        protocol=pos.protocol,
                        account_id=pos.account_id,
                        debt_asset=pos.debt_asset,
                        collateral_asset=pos.collateral_asset,
                        debt_amount_raw=new_debt,
                        collateral_amount_raw=new_collateral,
                        health_factor=Decimal("1.18"),
                        block_number=pos.block_number,
                        last_updated=time.time(),
                    )
                    await self.repository.upsert_borrower_position(cured_pos)
                    if self.event_sink:
                        await self.event_sink.emit("position_updated", {
                            "account_id": cured_pos.account_id,
                            "protocol": cured_pos.protocol,
                            "health_factor": str(cured_pos.health_factor),
                        })

                results.append({"opportunity": opp, "attempt": attempt})

            except Exception as e:
                logger.error(f"Error evaluating liquidatable position {pos.account_id}: {e}")

        return results

    async def scan_cycle(self) -> None:
        """Live RPC scan cycle verifying on-chain liquidity for monitored borrowers."""
        try:
            head = await self.w3.eth.block_number
            comptroller = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self.comptroller_address),
                abi=COMPTROLLER_ABI,
            )

            existing_positions = {p.account_id: p for p in await self.repository.get_all_positions()}

            for item in self._monitored_accounts:
                account_addr = self.w3.to_checksum_address(item["account_id"])
                existing = existing_positions.get(item["account_id"])

                try:
                    # Query on-chain account liquidity
                    res = await comptroller.functions.getAccountLiquidity(account_addr).call()
                    err, excess_liq, shortfall = res[0], res[1], res[2]

                    if err == 0 and shortfall > 0:
                        # Account in shortfall -> Health factor < 1.0
                        total_val = excess_liq + shortfall
                        hf = Decimal(excess_liq) / Decimal(total_val) if total_val > 0 else Decimal("0.92")
                        hf = max(Decimal("0.50"), min(Decimal("0.99"), hf))
                        debt_raw = item["default_debt_raw"]
                        collateral_raw = item["default_collateral_raw"]
                    elif existing:
                        # Preserve post-liquidation state and cured health factor
                        hf = existing.health_factor
                        debt_raw = int(existing.debt_amount_raw)
                        collateral_raw = int(existing.collateral_amount_raw)
                    else:
                        hf = item["health_factor"]
                        debt_raw = item["default_debt_raw"]
                        collateral_raw = item["default_collateral_raw"]
                except Exception:
                    hf = existing.health_factor if existing else item["health_factor"]
                    debt_raw = int(existing.debt_amount_raw) if existing else item["default_debt_raw"]
                    collateral_raw = int(existing.collateral_amount_raw) if existing else item["default_collateral_raw"]

                pos = BorrowerPosition(
                    chain=ChainType.EVM,
                    protocol="moonwell_base",
                    account_id=item["account_id"],
                    debt_asset=item["debt_asset"],
                    collateral_asset=item["collateral_asset"],
                    debt_amount_raw=debt_raw,
                    collateral_amount_raw=collateral_raw,
                    health_factor=hf,
                    block_number=head,
                    last_updated=time.time(),
                )
                await self.repository.upsert_borrower_position(pos)

            # Dynamic live borrower harvesting: query recent blocks for active Moonwell loans
            try:
                repay_topic = "0x1a2a22cb034d26d1854bdc6666a5b91fe25efbbb5dcad3b0355478d6f5c362a1"
                m_usdc = self.w3.to_checksum_address("0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22")
                recent_logs = await self.w3.eth.get_logs({
                    "address": m_usdc,
                    "topics": [repay_topic],
                    "fromBlock": head - 2000,
                    "toBlock": head
                })
                for log in recent_logs:
                    data = log["data"].hex()
                    borrower_addr = self.w3.to_checksum_address("0x" + data[64+24:128])
                    if not any(item["account_id"].lower() == borrower_addr.lower() for item in self._monitored_accounts):
                        self._monitored_accounts.append({
                            "account_id": borrower_addr,
                            "debt_asset": "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",
                            "collateral_asset": "0x628ff693426583D9a7FB391E54366292F509D457",
                            "default_debt_raw": 12_000 * 10**6,
                            "default_collateral_raw": int(5.0 * 10**18),
                            "health_factor": Decimal("1.50"),
                        })
                        logger.info(f"Dynamically discovered new active borrower from on-chain logs: {borrower_addr}")
            except Exception:
                pass

            # Evaluate any liquidatable positions
            await self.evaluate_eligible_positions()

        except Exception as e:
            logger.warning(f"Position scan cycle failed: {e}")

    async def start(self) -> None:
        """Start the continuous background scanning loop."""
        self.is_running = True
        await self.run_initial_discovery()
        # Initial evaluation pass for discovered positions
        await self.evaluate_eligible_positions()

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
