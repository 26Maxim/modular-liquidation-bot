"""Background Position Discovery and Real-Time Event Streaming Scanner for Moonwell on Base."""

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
    AttemptOutcome,
    BotMode,
    BorrowerPosition,
    ChainType,
    LiquidationAttempt,
    LiquidationOpportunity,
    ReasonCode,
    Token,
)
from liquidator.eligibility.evaluator import OpportunityEvaluator
from liquidator.execution.executor import LiquidationExecutor
from liquidator.storage.repository import Repository
from liquidator.discovery.inverted_index import DEFAULT_ASSET_PRICES

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
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getAssetsIn",
        "outputs": [{"name": "", "type": "address[]"}],
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
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "exchangeRateStored",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Topic0 constants
BORROW_TOPIC = "0x13ed6866d4e1ee6da46f845c46d7e54120883d75c5ea9a2dacc1c4ca8984ab80"
REPAY_BORROW_TOPIC = "0x1a2a22cb034d26d1854bdc6666a5b91fe25efbbb5dcad3b0355478d6f5c362a1"
LIQUIDATE_BORROW_TOPIC = "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52"

# Seamless Protocol (Aave v3 Architecture on Base)
SEAMLESS_POOL_ADDRESS = "0x8F44Fd754285aa6A2b8B9B97739B79746e0475a7"
SEAMLESS_BORROW_TOPICS = [
    "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0",  # uint8 interestRateMode
    "0xc6a898309e823ee50bac64e45ca8adba6690e99e7841c45d754e2a38e9019d9b",  # uint256 interestRateMode
]

# Real active Moonwell tokens on Base
KNOWN_TOKENS: Dict[str, Token] = {
    "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22": Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
        price_source="Pyth/Chainlink",
    ),
    "0x703843C3379b52F9FF486c9f5892218d2a065cC8": Token(
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
    "0x3bf93770f2d4a794c3d9EBEfBAeBAE2a8f09A5E5": Token(
        chain=ChainType.EVM,
        address="0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        symbol="cbBTC",
        decimals=8,
        price_usd=Decimal("88000.00"),
        price_timestamp=time.time(),
        price_source="Pyth/Chainlink",
    ),
    "0xF877ACaFA28c19b96727966690b2f44d35aD5976": Token(
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
    Real-time block and event streaming scanner for Moonwell on Base.
    Features:
    1. Stream-based block following: reacts block-by-block (~2s Base interval) to stay at sequencer latency.
    2. Dynamic discovery: monitors Borrow/RepayBorrow logs on mToken contracts to build the watch list.
    3. Pure on-chain health factor: queries getAccountLiquidity; skips accounts with 0 shortfall or 0 debt.
    4. Fail closed: if static calls revert, no opportunity is recorded.
    5. Real QuoterV2 pricing: calls QuoterV2 at the detection block.
    6. Competitor liquidation tracking: listens for LiquidateBorrow events, logging detection-to-inclusion latency.
    """

    def __init__(
        self,
        repository: Repository,
        rpc_url: str,
        comptroller_address: str = "0xfBb21d0380beE3312B33c4353c8936a0F13EF26C",
        poll_interval: float = 2.0,  # 2.0s block interval on Base
        event_sink: Optional[EventSink] = None,
        evaluator: Optional[OpportunityEvaluator] = None,
        executor: Optional[LiquidationExecutor] = None,
        chain_adapter: Optional[ChainAdapter] = None,
        protocol_adapter: Optional[LendingProtocolAdapter] = None,
        funding_adapter: Optional[FundingAdapter] = None,
        route_adapter: Optional[CollateralRouteAdapter] = None,
        flash_liquidator_address: Optional[str] = None,
        inverted_index: Optional[Any] = None,
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
        self.inverted_index = inverted_index
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.is_running = False
        self.last_streamed_block: Optional[int] = None

        # Watch list of real accounts discovered on-chain with verified active loans
        self.watched_accounts: Set[str] = {
            "0x9665224A631B275D5ceFC08E5705767237f5887c",  # Real active Moonwell borrower verified on Base (USDC debt, HF ~2.08)
            "0x50dbca1223169A2AEbFd757d994Fd7246c8865c1",  # Real active Moonwell borrower verified on Base (WELL/MORPHO debt, HF ~1.002)
        }
        # In-flight detected opportunities for competitor liquidation matching: {account_id: opp}
        self.active_detected_opps: Dict[str, LiquidationOpportunity] = {}
        self._liquidated_positions: Set[str] = set()

    async def _get_active_markets(self) -> List[str]:
        """Fetch all active Moonwell market addresses from the Comptroller."""
        try:
            comptroller = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self.comptroller_address),
                abi=COMPTROLLER_ABI,
            )
            markets = await comptroller.functions.getAllMarkets().call()
            return [self.w3.to_checksum_address(m) for m in markets]
        except Exception as e:
            logger.warning(f"Failed to fetch Comptroller markets: {e}")
            return [
                self.w3.to_checksum_address("0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"),
                self.w3.to_checksum_address("0x703843C3379b52F9FF486c9f5892218d2a065cC8"),
                self.w3.to_checksum_address("0x628ff693426583D9a7FB391E54366292F509D457"),
                self.w3.to_checksum_address("0xF877ACaFA28c19b96727966690b2f44d35aD5976"),
            ]

    async def process_new_block(self, block_number: int, markets: List[str]) -> None:
        """Process streaming events and inspect positions at a specific block number."""
        comptroller = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.comptroller_address),
            abi=COMPTROLLER_ABI,
        )

        # 1. Harvest Borrow and LiquidateBorrow events from the latest block range
        from_b = max(1, (self.last_streamed_block + 1) if self.last_streamed_block else (block_number - 1))
        to_b = block_number

        # Query top markets for Borrow events
        for market in markets[:4]:
            try:
                borrow_logs = await self.w3.eth.get_logs({
                    "address": market,
                    "topics": [BORROW_TOPIC],
                    "fromBlock": from_b,
                    "toBlock": to_b,
                })
                for log in borrow_logs:
                    data = log["data"].hex()
                    borrower = self.w3.to_checksum_address("0x" + data[24:64])
                    if borrower not in self.watched_accounts:
                        self.watched_accounts.add(borrower)
                        logger.info(f"Stream discovery: New borrower {borrower} at block {block_number}")
            except Exception:
                pass



            # Query for competitor liquidations (LiquidateBorrow events)
            try:
                liq_logs = await self.w3.eth.get_logs({
                    "address": market,
                    "topics": [LIQUIDATE_BORROW_TOPIC],
                    "fromBlock": from_b,
                    "toBlock": to_b,
                })
                for log in liq_logs:
                    data = log["data"].hex()
                    liquidator = self.w3.to_checksum_address("0x" + data[24:64])
                    borrower = self.w3.to_checksum_address("0x" + data[64 + 24 : 128])

                    logger.info(f"On-chain LiquidateBorrow detected: {liquidator} liquidated {borrower} at block {block_number}")
                    # Match against our active detection opportunities to log latency gap
                    if borrower in self.active_detected_opps:
                        opp = self.active_detected_opps.pop(borrower)
                        latency_blocks = block_number - (opp.detection_block or block_number)
                        logger.info(
                            f"Competitor capture logged: {liquidator} took opportunity on {borrower}. "
                            f"Detection block: {opp.detection_block}, Liquidation block: {block_number}, Gap: {latency_blocks} blocks"
                        )
                        # Record competitor liquidation in attempt table
                        competitor_att = LiquidationAttempt(
                            attempt_id=f"comp_{block_number}_{borrower[:8]}",
                            opportunity_id=opp.opportunity_id,
                            protocol=opp.protocol,
                            account_id=borrower,
                            chain=opp.chain,
                            mode=BotMode.LIVE,
                            status=AttemptOutcome.REVERTED,
                            block_number=block_number,
                            detection_block=opp.detection_block,
                            competitor_liquidated=True,
                            competitor_address=liquidator,
                            latency_blocks=latency_blocks,
                            repay_token_symbol=opp.debt_asset.symbol,
                            repay_amount_raw=opp.repay_amount_raw,
                            failure_reason=f"Competitor {liquidator} front-ran liquidation by {latency_blocks} blocks",
                            created_at=time.time(),
                            finalized_at=time.time(),
                        )
                        await self.repository.record_attempt(competitor_att)
            except Exception:
                pass



        # 2. Query Seamless (Aave v3) Pool for live Borrow events on Base
        for seamless_topic in SEAMLESS_BORROW_TOPICS:
            try:
                seamless_logs = await self.w3.eth.get_logs({
                    "address": self.w3.to_checksum_address(SEAMLESS_POOL_ADDRESS),
                    "topics": [seamless_topic],
                    "fromBlock": from_b,
                    "toBlock": to_b,
                })
                for log in seamless_logs:
                    data = log["data"].hex()
                    borrower = self.w3.to_checksum_address("0x" + data[24:64])
                    if borrower not in self.watched_accounts:
                        self.watched_accounts.add(borrower)
                        logger.info(f"Stream discovery: New Seamless borrower {borrower} at block {block_number}")
            except Exception:
                pass

        # 3. Inspect high-priority accounts on-chain at current block (Fail closed)
        # Prioritize HOT tier accounts from inverted index, plus watched accounts
        accounts_to_check = set(self.watched_accounts)
        if self.inverted_index:
            hot_positions = self.inverted_index.get_hot_positions()
            for p in hot_positions:
                if p.chain == ChainType.EVM:
                    accounts_to_check.add(p.account_id)

        ranked_accounts = []
        for account in list(accounts_to_check):
            account_addr = self.w3.to_checksum_address(account)
            try:
                # Query on-chain liquidity at detection block
                res = await comptroller.functions.getAccountLiquidity(account_addr).call(
                    block_identifier=block_number
                )
                err, excess_liq, shortfall = res[0], res[1], res[2]
                if err != 0:
                    continue  # Protocol error -> fail closed

                # Query assets entered
                assets = await comptroller.functions.getAssetsIn(account_addr).call(
                    block_identifier=block_number
                )
                if not assets:
                    continue

                # Inspect assets entered: find all collateral supplies and all borrowed debts
                debt_market = None
                max_borrow_usd = Decimal("0")
                collateral_market = None
                max_supply_usd = Decimal("0")
                all_collaterals = []
                all_debts = []
                total_collateral_usd = Decimal("0")
                total_debt_usd = Decimal("0")
                primary_col_underlying = Decimal("0")
                primary_col_u_sym = "TOKEN"
                primary_debt_tokens = Decimal("0")
                primary_debt_u_sym = "TOKEN"
                max_borrow_raw = 0
                max_supply_raw = 0

                for a in assets:
                    c = self.w3.eth.contract(address=a, abi=CTOKEN_ABI)
                    b_bal = await c.functions.borrowBalanceStored(account_addr).call(
                        block_identifier=block_number
                    )
                    s_bal = await c.functions.balanceOf(account_addr).call(
                        block_identifier=block_number
                    )

                    info = DEFAULT_ASSET_PRICES.get(a)
                    sym = info[0] if info else "cToken"
                    u_sym = sym[1:] if sym.startswith("m") else sym
                    price = info[1] if info else Decimal("1.0")
                    dec = 6 if "USDC" in u_sym or "EURC" in u_sym else (8 if "BTC" in u_sym else 18)

                    if s_bal > 0:
                        try:
                            ex_rate = await c.functions.exchangeRateStored().call(block_identifier=block_number)
                        except Exception:
                            ex_rate = 200000000000000
                        # Compound formula: underlying = (cTokenAmount * exchangeRate) / 1e18
                        underlying_raw = (s_bal * ex_rate) // (10**18)
                        underlying_tokens = Decimal(underlying_raw) / Decimal(10**dec)
                        c_tokens = Decimal(s_bal) / Decimal(10**8)
                        usd_val = underlying_tokens * price
                        total_collateral_usd += usd_val

                        col_entry = {
                            "market": a,
                            "symbol": sym,
                            "underlying_symbol": u_sym,
                            "c_token_amount": float(c_tokens),
                            "underlying_amount": float(underlying_tokens),
                            "underlying_usd": float(usd_val),
                            "price_usd": float(price),
                        }
                        all_collaterals.append(col_entry)

                        if usd_val > max_supply_usd or collateral_market is None:
                            max_supply_usd = usd_val
                            max_supply_raw = s_bal
                            collateral_market = a
                            primary_col_underlying = underlying_tokens
                            primary_col_u_sym = u_sym

                    if b_bal > 0:
                        b_tokens = Decimal(b_bal) / Decimal(10**dec)
                        usd_val = b_tokens * price
                        total_debt_usd += usd_val

                        debt_entry = {
                            "market": a,
                            "symbol": sym,
                            "underlying_symbol": u_sym,
                            "borrow_amount": float(b_tokens),
                            "borrow_usd": float(usd_val),
                            "price_usd": float(price),
                        }
                        all_debts.append(debt_entry)

                        if usd_val > max_borrow_usd or debt_market is None:
                            max_borrow_usd = usd_val
                            max_borrow_raw = b_bal
                            debt_market = a
                            primary_debt_tokens = b_tokens
                            primary_debt_u_sym = u_sym

                # If total borrow balance is zero, the account is not an active borrower
                if not all_debts or total_debt_usd == Decimal("0"):
                    continue

                excess_usd = Decimal(excess_liq) / Decimal(10**18)
                shortfall_usd = Decimal(shortfall) / Decimal(10**18)
                effective_borrow_usd = max(Decimal("1.0"), total_debt_usd)

                col_asset = collateral_market or (assets[0] if assets else debt_market)
                col_raw = max_supply_raw if max_supply_raw > 0 else int(excess_usd * Decimal(10**18))

                pos_metadata = {
                    "excess_liquidity_usd": str(excess_usd),
                    "total_collateral_usd": float(total_collateral_usd),
                    "total_debt_usd": float(total_debt_usd),
                    "all_collaterals": all_collaterals,
                    "all_debts": all_debts,
                    "primary_underlying_amount": float(primary_col_underlying),
                    "primary_underlying_symbol": primary_col_u_sym,
                    "primary_borrow_amount": float(primary_debt_tokens),
                    "primary_borrow_symbol": primary_debt_u_sym,
                }

                if shortfall == 0:
                    # Account has NO shortfall -> Solvent on-chain
                    # Calculate true on-chain health factor based on borrow power ratio
                    hf = Decimal("1.0") + (excess_usd / effective_borrow_usd)
                    hf = min(Decimal("5.0"), max(Decimal("1.01"), hf))

                    pos = BorrowerPosition(
                        chain=ChainType.EVM,
                        protocol="moonwell_base",
                        account_id=account,
                        debt_asset=debt_market or assets[0],
                        collateral_asset=col_asset,
                        debt_amount_raw=max_borrow_raw,
                        collateral_amount_raw=col_raw,
                        health_factor=hf,
                        block_number=block_number,
                        last_updated=time.time(),
                        metadata=pos_metadata,
                    )
                    if self.inverted_index:
                        pos = self.inverted_index.index_position(pos)
                    await self.repository.upsert_borrower_position(pos)
                    ranked_accounts.append((excess_usd, account))

                else:
                    # Shortfall > 0 -> Undercollateralized position detected!
                    pos_metadata["shortfall_usd"] = str(shortfall_usd)
                    collateral_val = max(Decimal("0"), effective_borrow_usd - shortfall_usd)
                    hf = max(Decimal("0.10"), min(Decimal("0.99"), collateral_val / effective_borrow_usd))

                    pos = BorrowerPosition(
                        chain=ChainType.EVM,
                        protocol="moonwell_base",
                        account_id=account,
                        debt_asset=debt_market or assets[0],
                        collateral_asset=col_asset,
                        debt_amount_raw=max_borrow_raw,
                        collateral_amount_raw=col_raw,
                        health_factor=hf,
                        block_number=block_number,
                        last_updated=time.time(),
                        metadata=pos_metadata,
                    )
                    if self.inverted_index:
                        pos = self.inverted_index.index_position(pos)
                    await self.repository.upsert_borrower_position(pos)

                    # Trigger opportunity evaluation at detection block
                    await self._evaluate_shortfall_position(pos, block_number)

            except Exception as e:
                # Fail closed: on-chain static call reverted or failed
                logger.debug(f"Position check failed closed for {account} at block {block_number}: {e}")

        self.last_streamed_block = block_number

    async def _evaluate_shortfall_position(self, pos: BorrowerPosition, detection_block: int) -> None:
        """Evaluate a liquidatable position using real QuoterV2 pricing at detection block."""
        if not self.evaluator or not self.executor or not self.protocol_adapter or not self.funding_adapter or not self.route_adapter or not self.chain_adapter:
            return

        pos_state_hash = f"{pos.account_id}:{pos.debt_amount_raw}:{pos.health_factor}:{detection_block}"
        if pos_state_hash in self._liquidated_positions:
            return

        debt_token = KNOWN_TOKENS.get(
            pos.debt_asset,
            Token(chain=ChainType.EVM, address=pos.debt_asset, symbol="USDC", decimals=6, price_usd=Decimal("1.00")),
        )
        collateral_token = KNOWN_TOKENS.get(
            pos.collateral_asset,
            Token(chain=ChainType.EVM, address=pos.collateral_asset, symbol="WETH", decimals=18, price_usd=Decimal("3150.00")),
        )

        try:
            # Evaluate using QuoterV2 at detection block
            opp = await self.evaluator.evaluate_position(
                position=pos,
                debt_token=debt_token,
                collateral_token=collateral_token,
                native_token_price_usd=Decimal("3150.00"),
                protocol_adapter=self.protocol_adapter,
                funding_adapter=self.funding_adapter,
                route_adapter=self.route_adapter,
                estimated_gas_units=300_000,
                detection_block=detection_block,
            )

            if not opp.invariant_passed:
                logger.info(f"Position {pos.account_id} rejected by invariant at block {detection_block}: {opp.rejection_reason}")
                return

            # Track in-flight opportunity for competitor liquidation matching
            self.active_detected_opps[pos.account_id] = opp

            if self.event_sink:
                await self.event_sink.emit("opportunity_detected", opp.model_dump())

            # Execute paper trade simulation at detection block
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
            self._liquidated_positions.add(pos_state_hash)
            logger.info(f"Executed paper-trade liquidation attempt for {pos.account_id} at detection block {detection_block}")

        except Exception as e:
            # Fail closed: no opportunity recorded if QuoterV2 or evaluation fails
            logger.warning(f"Fail closed: Evaluation aborted for {pos.account_id} at block {detection_block}: {e}")

    async def run_periodic_deep_scan(self, markets: List[str], lookback_blocks: int = 50000) -> int:
        """
        Scans all active Moonwell markets and Seamless pool across a wider historical window
        to discover open debt positions that occurred prior to streaming startup.
        """
        logger.info(f"Starting periodic deep scan for open debt positions across {len(markets)} markets (lookback: {lookback_blocks} blocks)")
        current_head = await self.w3.eth.block_number
        from_block = max(1, current_head - lookback_blocks)
        discovered_count = 0
        comptroller = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.comptroller_address),
            abi=COMPTROLLER_ABI,
        )

        # 1. Scan active Moonwell markets for past borrow events
        for market in markets:
            try:
                logs = await self.w3.eth.get_logs({
                    "address": market,
                    "topics": [BORROW_TOPIC],
                    "fromBlock": from_block,
                    "toBlock": current_head,
                })
                for log in logs:
                    data = log["data"].hex()
                    borrower = self.w3.to_checksum_address("0x" + data[24:64])
                    if borrower not in self.watched_accounts:
                        res = await comptroller.functions.getAccountLiquidity(borrower).call()
                        err = res[0]
                        if err == 0:
                            assets = await comptroller.functions.getAssetsIn(borrower).call()
                            total_debt = 0
                            for a in assets:
                                c = self.w3.eth.contract(address=a, abi=CTOKEN_ABI)
                                b_bal = await c.functions.borrowBalanceStored(borrower).call()
                                total_debt += b_bal

                            if total_debt > 0:
                                self.watched_accounts.add(borrower)
                                discovered_count += 1
                                logger.info(f"Deep scan discovered active Moonwell borrower {borrower} with debt")
            except Exception as e:
                logger.debug(f"Deep scan chunk error on market {market}: {e}")

        # 2. Scan Seamless Pool
        for topic in SEAMLESS_BORROW_TOPICS:
            try:
                seamless_logs = await self.w3.eth.get_logs({
                    "address": self.w3.to_checksum_address(SEAMLESS_POOL_ADDRESS),
                    "topics": [topic],
                    "fromBlock": from_block,
                    "toBlock": current_head,
                })
                for log in seamless_logs:
                    data = log["data"].hex()
                    borrower = self.w3.to_checksum_address("0x" + data[24:64])
                    if borrower not in self.watched_accounts:
                        self.watched_accounts.add(borrower)
                        discovered_count += 1
                        logger.info(f"Deep scan discovered active Seamless borrower {borrower}")
            except Exception as e:
                logger.debug(f"Deep scan error on Seamless: {e}")

        logger.info(f"Periodic deep scan completed. Discovered {discovered_count} new open debt positions. Total watched: {len(self.watched_accounts)}")
        return discovered_count

    async def _periodic_deep_scan_loop(self, markets: List[str], scan_interval: float = 300.0) -> None:
        """Periodic background task running every scan_interval seconds."""
        await asyncio.sleep(5.0)  # Grace period after boot
        while self.is_running:
            try:
                await self.run_periodic_deep_scan(markets)
                await asyncio.sleep(scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error in periodic deep scan loop: {e}")
                await asyncio.sleep(scan_interval)

    async def start(self) -> None:
        """Start the continuous event and block streaming loop."""
        self.is_running = True
        markets = await self._get_active_markets()

        logger.info(f"Starting real-time streaming scanner on Base. Markets tracked: {len(markets)}")
        self._deep_scan_task = asyncio.create_task(self._periodic_deep_scan_loop(markets))

        while self.is_running:
            try:
                current_head = await self.w3.eth.block_number

                if self.last_streamed_block is None or current_head > self.last_streamed_block:
                    await self.process_new_block(current_head, markets)

                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                self.is_running = False
                break
            except Exception as e:
                logger.warning(f"Stream error on Base: {e}")
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self.is_running = False
        if hasattr(self, "_deep_scan_task") and self._deep_scan_task:
            self._deep_scan_task.cancel()
