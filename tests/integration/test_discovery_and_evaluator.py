"""Integration tests for discovery, oracle watcher, and opportunity evaluation."""

import time
from decimal import Decimal

import pytest
from liquidator.core.models import (
    BorrowerPosition,
    ChainType,
    OraclePriceUpdate,
    RiskParameters,
    Token,
)
from liquidator.discovery.oracle_watcher import OracleUpdateWatcher
from liquidator.eligibility.evaluator import OpportunityEvaluator
from liquidator.funding.balancer_flash_loan import BalancerFlashLoanAdapter
from liquidator.pricing.uniswap_v3_quoter import UniswapV3RouteAdapter
from liquidator.protocols.moonwell_adapter import MoonwellAdapter
from liquidator.storage.models import Base
from liquidator.storage.repository import Repository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def repo():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    repository = Repository(session_factory)
    yield repository
    await engine.dispose()


@pytest.mark.asyncio
async def test_oracle_watcher_health_and_prioritized_evaluation(repo: Repository):
    """Verify OracleUpdateWatcher tracks health and triggers position re-evaluation."""
    usdc = Token(
        chain=ChainType.EVM,
        address="0xUSDC",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )
    weth = Token(
        chain=ChainType.EVM,
        address="0xWETH",
        symbol="WETH",
        decimals=18,
        price_usd=Decimal("3000.00"),
        price_timestamp=time.time(),
    )

    # Monitored position in repository
    pos = BorrowerPosition(
        chain=ChainType.EVM,
        protocol="moonwell_base",
        account_id="0xBorrowerWatcherTest",
        debt_asset=usdc.address,
        collateral_asset=weth.address,
        debt_amount_raw=5_000 * 10**6,
        collateral_amount_raw=2 * 10**18,
        health_factor=Decimal("0.95"),  # Liquidatable!
        block_number=19_000_000,
    )
    await repo.upsert_borrower_position(pos)

    # Setup Oracle watcher with callback tracking
    re_evaluated_positions = []

    async def handle_price_update(update: OraclePriceUpdate):
        # Fetch affected positions
        positions = await repo.get_positions_by_asset(update.asset_address)
        re_evaluated_positions.extend(positions)

    watcher = OracleUpdateWatcher(
        chain=ChainType.EVM,
        max_staleness_seconds=300,
        on_price_update=handle_price_update,
    )
    watcher.register_asset("WETH", weth.address)

    # Ingest price drop event (WETH drops from $3000 to $2600)
    update_event = OraclePriceUpdate(
        chain=ChainType.EVM,
        asset_address=weth.address,
        symbol="WETH",
        price_usd=Decimal("2600.00"),
        timestamp=time.time(),
        source="ChainlinkAggregator",
    )
    await watcher.notify_price_update(update_event)

    # Assert health tracking updated
    assert watcher.total_updates_received == 1
    assert watcher.is_stale is False
    assert watcher.lag_seconds < 1.0

    # Assert callback was triggered for affected position holding WETH
    assert len(re_evaluated_positions) == 1
    assert re_evaluated_positions[0].account_id == "0xBorrowerWatcherTest"


@pytest.mark.asyncio
async def test_end_to_end_opportunity_evaluation(repo: Repository):
    """Verify opportunity evaluator full pass on liquidatable position."""
    risk_params = RiskParameters(
        min_expected_profit_usd=Decimal("15.00"),
        max_repay_per_liquidation_usd=Decimal("25000.00"),
    )
    evaluator = OpportunityEvaluator(repository=repo, risk_params=risk_params)

    protocol_adapter = MoonwellAdapter()
    funding_adapter = BalancerFlashLoanAdapter()
    route_adapter = UniswapV3RouteAdapter()

    usdc = Token(
        chain=ChainType.EVM,
        address="0xUSDC",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )
    weth = Token(
        chain=ChainType.EVM,
        address="0xWETH",
        symbol="WETH",
        decimals=18,
        price_usd=Decimal("3000.00"),
        price_timestamp=time.time(),
    )

    pos = BorrowerPosition(
        chain=ChainType.EVM,
        protocol="moonwell_base",
        account_id="0xBorrowerProfitTest",
        debt_asset=usdc.address,
        collateral_asset=weth.address,
        debt_amount_raw=10_000 * 10**6,  # 10,000 USDC debt
        collateral_amount_raw=5 * 10**18,   # 5 WETH ($15,000)
        health_factor=Decimal("0.90"),
        block_number=20_000_000,
    )

    opp = await evaluator.evaluate_position(
        position=pos,
        debt_token=usdc,
        collateral_token=weth,
        native_token_price_usd=Decimal("3000.00"),
        protocol_adapter=protocol_adapter,
        funding_adapter=funding_adapter,
        route_adapter=route_adapter,
        estimated_gas_units=300_000,
    )

    assert opp.repay_amount_raw == 5_000 * 10**6  # 50% close factor
    assert opp.invariant_passed is True
    assert opp.expected_net_profit_usd > Decimal("15.00")
    assert opp.rejection_reason is None

    # Saved in repository
    saved = await repo.get_recent_opportunities()
    assert len(saved) == 1
    assert saved[0].opportunity_id == opp.opportunity_id
