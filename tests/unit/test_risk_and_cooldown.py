"""Unit tests for risk policies, circuit breakers, simulation cooldown, and state-change guard."""

import time
from decimal import Decimal

import pytest
from liquidator.core.models import (
    ChainType,
    LiquidationOpportunity,
    ReasonCode,
    RiskParameters,
    Token,
)
from liquidator.risk.policy_engine import PolicyEngine
from liquidator.storage.models import Base
from liquidator.storage.repository import Repository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def in_memory_repo():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    repo = Repository(session_factory)
    yield repo
    await engine.dispose()


@pytest.mark.asyncio
async def test_simulation_cooldown_and_state_change_guard(in_memory_repo: Repository):
    """Verify simulation cooldown suppresses repeated attempts unless oracle price or reserves change."""
    protocol = "moonwell_base"
    account = "0xBorrowerCooldownTest"
    debt = "0xUSDC"
    collateral = "0xWETH"
    initial_price = Decimal("3000.00")
    initial_reserves_hash = "1000:2000"

    # Step 1: Initial state is not in cooldown
    in_cd = await in_memory_repo.is_in_cooldown(
        protocol=protocol,
        account_id=account,
        debt_asset=debt,
        collateral_asset=collateral,
        current_oracle_price=initial_price,
        current_reserves_hash=initial_reserves_hash,
        cooldown_duration=60,
    )
    assert in_cd is False

    # Step 2: Set cooldown after failed simulation
    await in_memory_repo.set_cooldown(
        protocol=protocol,
        account_id=account,
        debt_asset=debt,
        collateral_asset=collateral,
        oracle_price=initial_price,
        reserves_hash=initial_reserves_hash,
        reason_code=ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE,
    )

    # Step 3: Check cooldown again with unchanged state -> MUST BE TRUE
    in_cd_now = await in_memory_repo.is_in_cooldown(
        protocol=protocol,
        account_id=account,
        debt_asset=debt,
        collateral_asset=collateral,
        current_oracle_price=initial_price,
        current_reserves_hash=initial_reserves_hash,
        cooldown_duration=60,
    )
    assert in_cd_now is True

    # Step 4: State-change guard: Oracle price updates -> MUST INVALIDATE COOLDOWN!
    updated_price = Decimal("2950.00")
    in_cd_after_price_change = await in_memory_repo.is_in_cooldown(
        protocol=protocol,
        account_id=account,
        debt_asset=debt,
        collateral_asset=collateral,
        current_oracle_price=updated_price,
        current_reserves_hash=initial_reserves_hash,
        cooldown_duration=60,
    )
    assert in_cd_after_price_change is False


@pytest.mark.asyncio
async def test_rejection_log_deduplication_without_volatile_keys(in_memory_repo: Repository):
    """Verify rejection logs deduplicate on (protocol, account_id, reason_code) without volatile gas values in key."""
    protocol = "moonwell_base"
    account = "0xBorrowerDedup"
    reason = ReasonCode.GAS_EXCEEDS_PROFIT

    # Log first occurrence with gas price 2.5 gwei
    await in_memory_repo.log_rejection(
        protocol=protocol,
        account_id=account,
        reason_code=reason,
        details={"gas_price_gwei": "2.5", "gas_wei": 2500000000},
    )

    # Log second occurrence with volatile gas price 3.8 gwei
    await in_memory_repo.log_rejection(
        protocol=protocol,
        account_id=account,
        reason_code=reason,
        details={"gas_price_gwei": "3.8", "gas_wei": 3800000000},
    )

    logs = await in_memory_repo.get_rejection_logs()
    # Must have exactly 1 record with count = 2
    assert len(logs) == 1
    assert logs[0].occurrence_count == 2
    assert logs[0].dedup_key == f"{protocol}:{account}:{reason.value}"


def test_policy_engine_circuit_breakers_and_notional_caps():
    """Verify policy engine limits, consecutive failure circuit breaker, and kill switch."""
    params = RiskParameters(
        max_repay_per_liquidation_usd=Decimal("10000.00"),
        min_expected_profit_usd=Decimal("10.00"),
        max_consecutive_failures=2,
    )
    engine = PolicyEngine(params)

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

    # Test 1: Opportunity exceeding notional cap ($15,000 > $10,000)
    oversized_opp = LiquidationOpportunity(
        opportunity_id="opp_large",
        chain=ChainType.EVM,
        protocol="moonwell_base",
        borrower_address="0xBorrowerBig",
        debt_asset=usdc,
        collateral_asset=weth,
        repay_amount_raw=15_000 * 10**6,  # $15,000
        expected_collateral_raw=10**18,
        expected_proceeds_raw=16_000 * 10**6,
        funding_source="balancer",
        funding_premium_raw=0,
        disposal_venue="uniswap_v3",
        estimated_gas_units=300000,
        estimated_gas_debt_raw=100000,
        estimated_gas_usd=Decimal("1.00"),
        expected_net_profit_debt_raw=900 * 10**6,
        expected_net_profit_usd=Decimal("900.00"),
        invariant_passed=True,
    )

    auth, reason, msg = engine.authorize_execution(oversized_opp)
    assert auth is False
    assert reason == ReasonCode.MAX_NOTIONAL_EXCEEDED

    # Test 2: Consecutive failures trip circuit breaker
    engine.record_attempt_result(success=False, net_pnl_usd=Decimal("-5.00"))
    assert engine.state.value == "RUNNING"
    engine.record_attempt_result(success=False, net_pnl_usd=Decimal("-5.00"))
    # Reached 2 consecutive failures -> Must be PAUSED!
    assert engine.state.value == "PAUSED"
    assert "consecutive failures" in engine.last_circuit_trip_reason

    # Test 3: Kill switch
    engine.trigger_kill_switch("Emergency test")
    assert engine.kill_switch_active is True
    assert engine.state.value == "KILLED"
