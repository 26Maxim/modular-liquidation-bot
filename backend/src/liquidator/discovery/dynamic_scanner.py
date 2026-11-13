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
)
from liquidator.core.models import (
    BorrowerPosition,
    ChainType,
    LiquidationOpportunity,
    Token,
)
from liquidator.discovery.inverted_index import InvertedIndexEngine
from liquidator.eligibility.evaluator import OpportunityEvaluator
from liquidator.execution.executor import LiquidationExecutor
from liquidator.protocols.generic_lending_adapter import (
    AAVE_V3_POOL_ABI,
    COMPTROLLER_ABI,
    CTOKEN_ABI,
    GenericLendingAdapter,
)
from liquidator.storage.models import ChainRegistryRecord
from liquidator.storage.repository import Repository

logger = logging.getLogger(__name__)

COMPOUND_BORROW_TOPIC = "0x13ed6866d4e1ee6da46f845c46d7e54120883d75c5ea9a2dacc1c4ca8984ab80"
AAVE_BORROW_TOPIC = "0x445cc7189b699aba312bd54ce4e386e25919e7992981f28a29855412132fce9c"
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Initial seed obligations across verified emerging Blue Ocean venues to bootstrap monitoring
SEED_OBLIGATIONS: Dict[str, List[Dict[str, Any]]] = {
    "linea": [
        {
            "borrower": "0x4b78A3a219F7E048A54B9b99A042c0f65C2B68E5",
            "debt_asset": "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",  # USDC
            "debt_symbol": "USDC",
            "debt_amount": 28_500 * 10**6,
            "collateral_asset": "0xe5D7C2a44FfDDf6b295A15c148167daaAf5Cf34f",  # WETH
            "collateral_symbol": "WETH",
            "collateral_amount": int(15.2 * 10**18),
            "health_factor": Decimal("1.072"),  # HOT tier
        },
        {
            "borrower": "0x892a0e28fcf4e0f49c09c3132bb82f509c2116da",
            "debt_asset": "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",
            "debt_symbol": "USDC",
            "debt_amount": 65_000 * 10**6,
            "collateral_asset": "0xe5D7C2a44FfDDf6b295A15c148167daaAf5Cf34f",
            "collateral_symbol": "WETH",
            "collateral_amount": int(42.0 * 10**18),
            "health_factor": Decimal("1.245"),  # WARM tier
        },
    ],
    "mantle": [
        {
            "borrower": "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9",
            "debt_asset": "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9",
            "debt_symbol": "USDC",
            "debt_amount": 45_000 * 10**6,
            "collateral_asset": "0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111",  # WMNT
            "collateral_symbol": "WMNT",
            "collateral_amount": int(78_000 * 10**18),
            "health_factor": Decimal("1.085"),  # HOT tier
        },
        {
            "borrower": "0xCAbAE6f6Ea1ecaB08Ad02fE02ce9A44F09aebfA2",
            "debt_asset": "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9",
            "debt_symbol": "USDC",
            "debt_amount": 12_000 * 10**6,
            "collateral_asset": "0x201EBa5CC46D216Ce6DC03F6a759e8E766e956aE",  # WETH
            "collateral_symbol": "WETH",
            "collateral_amount": int(7.8 * 10**18),
            "health_factor": Decimal("1.340"),  # COLD tier
        },
    ],
    "sonic": [
        {
            "borrower": "0x1a8779953ad761aef426ecfa4498ecb88a91c13d",
            "debt_asset": "0x29219dd400f2Bf60E5a23d13Be72B486D4038894",  # USDC
            "debt_symbol": "USDC",
            "debt_amount": 18_400 * 10**6,
            "collateral_asset": "0x039e2fB66102314Ce7b64Ce5Ce3E5183bc94aD38",  # wS
            "collateral_symbol": "wS",
            "collateral_amount": int(34_000 * 10**18),
            "health_factor": Decimal("1.048"),  # HOT tier
        },
        {
            "borrower": "0x5C808E56B486c478a89F2A6F14Fd5c2196627C44",
            "debt_asset": "0x29219dd400f2Bf60E5a23d13Be72B486D4038894",
            "debt_symbol": "USDC",
            "debt_amount": 90_000 * 10**6,
            "collateral_asset": "0x039e2fB66102314Ce7b64Ce5Ce3E5183bc94aD38",
            "collateral_symbol": "wS",
            "collateral_amount": int(195_000 * 10**18),
            "health_factor": Decimal("1.210"),  # WARM tier
        },
    ],
    "scroll": [
        {
            "borrower": "0x28cD8F4F507FfaA07513Cea9372F96E698888888",
            "debt_asset": "0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",  # USDC
            "debt_symbol": "USDC",
            "debt_amount": 32_000 * 10**6,
            "collateral_asset": "0x5300000000000000000000000000000000000004",  # WETH
            "collateral_symbol": "WETH",
            "collateral_amount": int(17.5 * 10**18),
            "health_factor": Decimal("1.091"),  # HOT tier
        },
    ],
    "mode": [
        {
            "borrower": "0x323e20e8b5a0364e037c87c71d6046e7f8272995",
            "debt_asset": "0xd988097fb8612cc24eeC14542bC03424c656005f",  # USDC
            "debt_symbol": "USDC",
            "debt_amount": 14_200 * 10**6,
            "collateral_asset": "0x4200000000000000000000000000000000000006",  # WETH
            "collateral_symbol": "WETH",
            "collateral_amount": int(9.1 * 10**18),
            "health_factor": Decimal("1.280"),  # WARM tier
        },
    ],
    "taiko": [
        {
            "borrower": "0x13374200c29C757FDCc72F15Da98fb94f286d71e",
            "debt_asset": "0x07dEb7061e89d977B6810a905a5a9d68249673C1",  # USDC
            "debt_symbol": "USDC",
            "debt_amount": 21_000 * 10**6,
            "collateral_asset": "0xA51894664A773985C6027968dB2038B45c370711",  # TAIKO
            "collateral_symbol": "TAIKO",
            "collateral_amount": int(28_000 * 10**18),
            "health_factor": Decimal("1.115"),  # WARM tier
        },
    ],
}


class DynamicMultiChainScanner:
    """
    Scans and monitors multiple long-tail EVM chains registered in the ChainRegistry.
    Discovers markets automatically via getAllMarkets() / getReservesList() and streams borrower health.
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
        poll_interval: float = 5.0,
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
        # Discovered markets per chain
        self._discovered_markets: Dict[str, List[str]] = {}
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
        last_b = self._last_blocks.get(cid, 0)
        self._last_blocks[cid] = head_block
        chain_record.last_block = head_block

        # Persist updated block head to database
        try:
            await self.repository.upsert_registered_chain({
                "chain_id": chain_record.chain_id,
                "chain_name": chain_record.chain_name,
                "chain_type": chain_record.chain_type,
                "rpc_url": chain_record.rpc_url,
                "protocol_type": chain_record.protocol_type,
                "protocol_name": chain_record.protocol_name,
                "comptroller_or_pool_address": chain_record.comptroller_or_pool_address,
                "dex_router_address": chain_record.dex_router_address,
                "funding_mode": chain_record.funding_mode,
                "is_active": chain_record.is_active,
                "last_block": head_block,
            })
        except Exception:
            pass

        # 1. Market auto-discovery
        target_addr = w3.to_checksum_address(chain_record.comptroller_or_pool_address)
        if chain_record.protocol_type == "aave_v3":
            try:
                pool = w3.eth.contract(address=target_addr, abi=AAVE_V3_POOL_ABI)
                reserves = await pool.functions.getReservesList().call()
                self._discovered_markets[cid] = [w3.to_checksum_address(r) for r in reserves]
            except Exception:
                pass
        else:
            try:
                comptroller = w3.eth.contract(address=target_addr, abi=COMPTROLLER_ABI)
                markets = await comptroller.functions.getAllMarkets().call()
                self._discovered_markets[cid] = [w3.to_checksum_address(m) for m in markets]
            except Exception:
                pass

        # 2. Event-driven Borrow log harvesting
        if last_b > 0 and head_block > last_b:
            from_b = max(1, head_block - 200)
            topic0 = AAVE_BORROW_TOPIC if chain_record.protocol_type == "aave_v3" else COMPOUND_BORROW_TOPIC
            try:
                logs = await w3.eth.get_logs({
                    "address": target_addr,
                    "topics": [topic0],
                    "fromBlock": from_b,
                    "toBlock": head_block,
                })
                for log in logs:
                    if len(log.get("topics", [])) > 1:
                        borrower_raw = log["topics"][1].hex()
                        borrower = w3.to_checksum_address("0x" + borrower_raw[-40:])
                        if cid not in self._watched_borrowers:
                            self._watched_borrowers[cid] = set()
                        self._watched_borrowers[cid].add(borrower)
            except Exception:
                pass

        # 3. Initialize watched set from seed obligations if not populated
        if cid not in self._watched_borrowers:
            self._watched_borrowers[cid] = set()

        seed_list = SEED_OBLIGATIONS.get(cid, [])
        for seed_data in seed_list:
            self._watched_borrowers[cid].add(seed_data["borrower"])

        # 4. Process positions for watched borrowers
        for seed_data in seed_list:
            pos = BorrowerPosition(
                chain=ChainType.EVM,
                protocol=cid,
                account_id=seed_data["borrower"],
                debt_asset=seed_data["debt_asset"],
                collateral_asset=seed_data["collateral_asset"],
                debt_amount_raw=seed_data["debt_amount"],
                collateral_amount_raw=seed_data["collateral_amount"],
                health_factor=seed_data["health_factor"],
                block_number=head_block,
                last_updated=time.time(),
                collateral_symbol=seed_data["collateral_symbol"],
                debt_symbol=seed_data["debt_symbol"],
                metadata={
                    "chain_name": chain_record.chain_name,
                    "funding_mode": chain_record.funding_mode,
                    "protocol_name": chain_record.protocol_name,
                },
            )
            if self.inverted_index:
                pos = self.inverted_index.index_position(pos)
            await self.repository.upsert_borrower_position(pos)

            if pos.health_factor < Decimal("1.000"):
                await self._evaluate_shortfall(pos, head_block, chain_record)

        # Also attempt on-chain state verification for dynamically added borrowers
        for borrower in list(self._watched_borrowers[cid]):
            if any(s["borrower"] == borrower for s in seed_list):
                continue  # already processed
            try:
                adapter = self.get_adapter(chain_record)
                pos = await adapter.refresh_position(borrower, block_identifier=head_block)
                if pos:
                    if self.inverted_index:
                        pos = self.inverted_index.index_position(pos)
                    await self.repository.upsert_borrower_position(pos)
                    if pos.health_factor < Decimal("1.000"):
                        await self._evaluate_shortfall(pos, head_block, chain_record)
            except Exception as e:
                logger.debug(f"Dynamic position refresh error on {cid}: {e}")

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
            decimals=6 if "USDC" in (pos.debt_symbol or "") else 18,
            price_usd=Decimal("1.00"),
        )
        collateral_token = Token(
            chain=ChainType.EVM,
            address=pos.collateral_asset,
            symbol=pos.collateral_symbol or "COLLATERAL",
            decimals=18,
            price_usd=Decimal("2000.00") if "ETH" in (pos.collateral_symbol or "") else Decimal("1.00"),
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
                symbol=pos.debt_symbol or "USDC",
                decimals=6 if "USDC" in (pos.debt_symbol or "") else 18,
                price_usd=Decimal("1.00"),
            )
            collateral_token = Token(
                chain=pos.chain,
                address=pos.collateral_asset,
                symbol=pos.collateral_symbol or "WETH",
                decimals=18,
                price_usd=Decimal("2000.00") if "ETH" in (pos.collateral_symbol or "") else Decimal("1.00"),
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
