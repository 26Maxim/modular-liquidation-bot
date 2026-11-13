"""Dynamic Multi-Chain Position Discovery & Event Scanner for Long-Tail EVM Chains."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set
from web3 import AsyncHTTPProvider, AsyncWeb3

from liquidator.core.interfaces import (
    ChainAdapter,
    CollateralRouteAdapter,
    EventSink,
    FundingAdapter,
    LendingProtocolAdapter,
)
from liquidator.core.models import (
    BorrowerPosition,
    ChainType,
    LiquidationOpportunity,
    Token,
)
from liquidator.discovery.inverted_index import DEFAULT_ASSET_PRICES, InvertedIndexEngine
from liquidator.eligibility.evaluator import OpportunityEvaluator
from liquidator.execution.executor import LiquidationExecutor
from liquidator.protocols.generic_lending_adapter import COMPTROLLER_ABI, CTOKEN_ABI, GenericLendingAdapter
from liquidator.storage.models import ChainRegistryRecord
from liquidator.storage.repository import Repository

logger = logging.getLogger(__name__)

BORROW_TOPIC = "0x13ed6866d4e1ee6da46f845c46d7e54120883d75c5ea9a2dacc1c4ca8984ab80"


class DynamicMultiChainScanner:
    """
    Scans and monitors multiple long-tail EVM chains registered in the ChainRegistry.
    Discovers markets automatically via getAllMarkets() and streams borrower health.
    """

    def __init__(
        self,
        repository: Repository,
        inverted_index: InvertedIndexEngine,
        evaluator: Optional[OpportunityEvaluator] = None,
        executor: Optional[LiquidationExecutor] = None,
        funding_adapter: Optional[FundingAdapter] = None,
        route_adapter: Optional[CollateralRouteAdapter] = None,
        chain_adapter: Optional[ChainAdapter] = None,
        event_sink: Optional[EventSink] = None,
        poll_interval: float = 3.0,
    ):
        self.repository = repository
        self.inverted_index = inverted_index
        self.evaluator = evaluator
        self.executor = executor
        self.funding_adapter = funding_adapter
        self.route_adapter = route_adapter
        self.chain_adapter = chain_adapter
        self.event_sink = event_sink
        self.poll_interval = poll_interval
        self.is_running = False

        # Cache of protocol adapters per chain_id
        self._adapters: Dict[str, GenericLendingAdapter] = {}
        # Watched borrowers per chain
        self._watched_borrowers: Dict[str, Set[str]] = {}
        # Last scanned blocks per chain
        self._last_blocks: Dict[str, int] = {}
        # Active discovered opportunities
        self.active_detected_opps: Dict[str, LiquidationOpportunity] = {}
        self._liquidated_positions: Set[str] = set()

    def get_adapter(self, chain_record: ChainRegistryRecord) -> GenericLendingAdapter:
        cid = chain_record.chain_id
        if cid not in self._adapters:
            self._adapters[cid] = GenericLendingAdapter(
                protocol_id=chain_record.chain_id,
                chain_id=chain_record.chain_id,
                contract_address=chain_record.comptroller_or_pool_address,
                rpc_url=chain_record.rpc_url,
                protocol_type=chain_record.protocol_type,
                dex_router_address=chain_record.dex_router_address,
            )
        return self._adapters[cid]

    async def scan_chain_once(self, chain_record: ChainRegistryRecord) -> None:
        """Polls one active chain for new blocks and updates borrower health."""
        w3 = AsyncWeb3(AsyncHTTPProvider(chain_record.rpc_url))
        try:
            head_block = await w3.eth.block_number
        except Exception as e:
            logger.debug(f"RPC connection failed for {chain_record.chain_name}: {e}")
            return

        cid = chain_record.chain_id
        self._last_blocks[cid] = head_block
        chain_record.last_block = head_block

        comptroller_addr = w3.to_checksum_address(chain_record.comptroller_or_pool_address)
        comptroller = w3.eth.contract(address=comptroller_addr, abi=COMPTROLLER_ABI)

        # 1. Auto-discover all active markets on this protocol
        try:
            markets = await comptroller.functions.getAllMarkets().call()
        except Exception:
            markets = []

        if not markets:
            return

        if cid not in self._watched_borrowers:
            self._watched_borrowers[cid] = set()

        # 2. Check known or discovered borrowers on this chain
        watched = self._watched_borrowers[cid]
        for borrower in list(watched):
            b_addr = w3.to_checksum_address(borrower)
            try:
                err, excess_liq, shortfall = await comptroller.functions.getAccountLiquidity(b_addr).call(
                    block_identifier=head_block
                )
                if err != 0:
                    continue

                assets = await comptroller.functions.getAssetsIn(b_addr).call(block_identifier=head_block)
                if not assets:
                    continue

                debt_market = None
                max_borrow = 0
                collateral_market = None
                max_supply = 0

                for a in assets:
                    c = w3.eth.contract(address=a, abi=CTOKEN_ABI)
                    b = await c.functions.borrowBalanceStored(b_addr).call(block_identifier=head_block)
                    s = await c.functions.balanceOf(b_addr).call(block_identifier=head_block)
                    if b > max_borrow:
                        max_borrow = b
                        debt_market = a
                    if s > max_supply:
                        max_supply = s
                        collateral_market = a

                if max_borrow == 0:
                    continue

                excess_usd = Decimal(excess_liq) / Decimal(10**18)
                shortfall_usd = Decimal(shortfall) / Decimal(10**18)
                borrow_usd = max(Decimal("1.0"), Decimal(max_borrow) / Decimal(10**18))

                col_asset = collateral_market or assets[-1]
                debt_asset = debt_market or assets[0]

                if shortfall == 0:
                    hf = Decimal("1.0") + (excess_usd / borrow_usd)
                    hf = min(Decimal("5.0"), max(Decimal("1.01"), hf))
                else:
                    col_val = max(Decimal("0"), borrow_usd - shortfall_usd)
                    hf = max(Decimal("0.10"), min(Decimal("0.99"), col_val / borrow_usd))

                pos = BorrowerPosition(
                    chain=ChainType.EVM,
                    protocol=cid,
                    account_id=borrower,
                    debt_asset=debt_asset,
                    collateral_asset=col_asset,
                    debt_amount_raw=max_borrow,
                    collateral_amount_raw=max_supply,
                    health_factor=hf,
                    block_number=head_block,
                    last_updated=time.time(),
                    metadata={
                        "excess_liquidity_usd": str(excess_usd),
                        "chain_name": chain_record.chain_name,
                        "funding_mode": chain_record.funding_mode,
                    },
                )
                if self.inverted_index:
                    pos = self.inverted_index.index_position(pos)
                await self.repository.upsert_borrower_position(pos)

                if shortfall > 0:
                    await self._evaluate_shortfall(pos, head_block, chain_record)

            except Exception as e:
                logger.debug(f"Position check error on {chain_record.chain_name} for {borrower}: {e}")

    async def _evaluate_shortfall(
        self,
        pos: BorrowerPosition,
        detection_block: int,
        chain_record: ChainRegistryRecord,
    ) -> None:
        """Evaluates liquidatable position and executes simulation."""
        if not self.evaluator or not self.executor:
            return

        adapter = self.get_adapter(chain_record)
        debt_token = Token(
            chain=ChainType.EVM,
            address=pos.debt_asset,
            symbol=pos.debt_symbol or "DEBT",
            decimals=18,
            price_usd=Decimal("1.00"),
        )
        collateral_token = Token(
            chain=ChainType.EVM,
            address=pos.collateral_asset,
            symbol=pos.collateral_symbol or "COLLATERAL",
            decimals=18,
            price_usd=Decimal("1.00"),
        )

        try:
            opp = await self.evaluator.evaluate_position(
                position=pos,
                debt_token=debt_token,
                collateral_token=collateral_token,
                native_token_price_usd=Decimal("1.00"),
                protocol_adapter=adapter,
                funding_adapter=self.funding_adapter,
                route_adapter=self.route_adapter,
                estimated_gas_units=250_000,
                detection_block=detection_block,
            )

            if opp.invariant_passed and self.chain_adapter:
                await self.executor.process_opportunity(
                    opp=opp,
                    chain_adapter=self.chain_adapter,
                    tx_call_params={
                        "to": chain_record.comptroller_or_pool_address,
                        "simulated_dry_run": True,
                        "estimated_gas": 250_000,
                    },
                    signer_key=None,
                )
        except Exception as e:
            logger.debug(f"Evaluation failed on {chain_record.chain_name}: {e}")

    async def evaluate_eligible_positions(self, force: bool = False) -> List[Dict[str, Any]]:
        """Evaluates HOT tier positions across all active registered chains."""
        results = []
        if not self.evaluator or not self.executor:
            return results

        candidates = []
        if self.inverted_index:
            hot_positions = self.inverted_index.get_hot_positions()
            candidates.extend(hot_positions)
            if not candidates and force:
                query_res = self.inverted_index.query(sort_by="health_factor", page=1, page_size=5)
                for p_dict in query_res.get("items", []):
                    pos_id = p_dict.get("id")
                    if pos_id and pos_id in self.inverted_index._positions_by_id:
                        p_obj = self.inverted_index._positions_by_id[pos_id]
                        if p_obj not in candidates:
                            candidates.append(p_obj)

        for pos in candidates:
            debt_token = Token(
                chain=pos.chain,
                address=pos.debt_asset,
                symbol=pos.debt_symbol or "DEBT",
                decimals=18,
                price_usd=Decimal("1.00"),
            )
            collateral_token = Token(
                chain=pos.chain,
                address=pos.collateral_asset,
                symbol=pos.collateral_symbol or "COLLATERAL",
                decimals=18,
                price_usd=Decimal("1.00"),
            )

            try:
                adapter = self._adapters.get(pos.protocol)
                if not adapter:
                    chains = await self.repository.get_registered_chains()
                    rec = next((c for c in chains if c.chain_id == pos.protocol), None)
                    if rec:
                        adapter = self.get_adapter(rec)

                opp = await self.evaluator.evaluate_position(
                    position=pos,
                    debt_token=debt_token,
                    collateral_token=collateral_token,
                    native_token_price_usd=Decimal("1.00"),
                    protocol_adapter=adapter,
                    funding_adapter=self.funding_adapter,
                    route_adapter=self.route_adapter,
                    estimated_gas_units=250_000,
                    detection_block=pos.block_number,
                )

                attempt = None
                if opp.invariant_passed and self.chain_adapter:
                    attempt = await self.executor.process_opportunity(
                        opp=opp,
                        chain_adapter=self.chain_adapter,
                        tx_call_params={"simulated_dry_run": True, "estimated_gas": 250_000},
                        signer_key=None,
                    )

                results.append({"opportunity": opp, "attempt": attempt})
            except Exception as e:
                logger.debug(f"Evaluation error for {pos.account_id}: {e}")

        return results

    async def start(self) -> None:
        """Main scanning loop across all active registered chains."""
        self.is_running = True
        logger.info("Dynamic Multi-Chain Scanner started.")
        while self.is_running:
            try:
                active_chains = await self.repository.get_registered_chains(active_only=True)
                for chain_record in active_chains:
                    await self.scan_chain_once(chain_record)
            except Exception as e:
                logger.debug(f"Error in dynamic scanner loop: {e}")
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self.is_running = False
        logger.info("Dynamic Multi-Chain Scanner stopped.")
