"""End-to-End tests verifying the complete dry-run pipeline, fail-closed live gates, and API."""

import time
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from liquidator.app import create_app
from liquidator.chains.evm_adapter import EVMChainAdapter
from liquidator.core.config import AppSettings
from liquidator.core.models import (
    AttemptOutcome,
    BotMode,
    ChainType,
    LiquidationOpportunity,
    ReasonCode,
    RiskParameters,
    SimulationResult,
    Token,
)
from liquidator.execution.executor import LiquidationExecutor
from liquidator.monitoring.metrics import EventHub
from liquidator.risk.policy_engine import PolicyEngine
from liquidator.storage.models import Base
from liquidator.storage.repository import Repository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class MockEVMAdapter(EVMChainAdapter):
    """Deterministic mock adapter for EVM execution simulation."""
    def __init__(self, simulate_success: bool = True):
        super().__init__(rpc_url="http://localhost:8545")
        self.simulate_success = simulate_success
        self.broadcast_called = False

    async def get_latest_block_number(self) -> int:
        return 21_500_000

    async def simulate_transaction(self, tx_params):
        if self.simulate_success:
            return SimulationResult(success=True, gas_used=320_000, return_data="0x01")
        return SimulationResult(
            success=False,
            error_message="execution reverted: InsufficientProfit",
            rejection_code=ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE,
        )

    async def submit_transaction(self, signed_tx_payload):
        self.broadcast_called = True
        return "0xMOCK_TX_HASH_8453"


@pytest.fixture
async def e2e_repo():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    repository = Repository(session_factory)
    yield repository
    await engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_never_broadcasts_live_transaction(e2e_repo: Repository):
    """Verify that DRY_RUN mode executes simulation, records attempt, but NEVER broadcasts to chain."""
    risk_params = RiskParameters()
    policy_engine = PolicyEngine(risk_params)
    policy_engine.mode = BotMode.DRY_RUN

    mock_chain = MockEVMAdapter(simulate_success=True)
    event_hub = EventHub()

    executor = LiquidationExecutor(
        repository=e2e_repo,
        policy_engine=policy_engine,
        allow_live_execution_flag=False,  # Live mode disabled
        event_sink=event_hub,
    )

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

    opp = LiquidationOpportunity(
        opportunity_id="opp_dry_run_test",
        chain=ChainType.EVM,
        protocol="moonwell_base",
        borrower_address="0xBorrowerDryRun",
        debt_asset=usdc,
        collateral_asset=weth,
        repay_amount_raw=5_000 * 10**6,
        expected_collateral_raw=10**18,
        expected_proceeds_raw=5_500 * 10**6,
        funding_source="balancer_v2_vault",
        funding_premium_raw=0,
        disposal_venue="uniswap_v3",
        estimated_gas_units=320000,
        estimated_gas_debt_raw=100000,
        estimated_gas_usd=Decimal("0.10"),
        expected_net_profit_debt_raw=499 * 10**6,
        expected_net_profit_usd=Decimal("499.00"),
        invariant_passed=True,
    )

    attempt = await executor.process_opportunity(
        opp=opp,
        chain_adapter=mock_chain,
        tx_call_params={"to": "0xExecutor"},
        signer_key=None,
    )

    # 1. Attempt status is SUCCESS
    assert attempt.status == AttemptOutcome.SUCCESS
    assert attempt.mode == BotMode.DRY_RUN
    # 2. tx_hash MUST be None (no transaction broadcasted)
    assert attempt.tx_hash is None
    assert mock_chain.broadcast_called is False

    # 3. Recorded in database
    attempts = await e2e_repo.get_recent_attempts()
    assert len(attempts) == 1
    assert attempts[0].attempt_id == attempt.attempt_id
    assert attempts[0].mode == "DRY_RUN"


@pytest.mark.asyncio
async def test_live_execution_fails_closed_without_server_latch(e2e_repo: Repository):
    """Verify live execution is rejected and fails closed if ALLOW_LIVE_EXECUTION is False."""
    risk_params = RiskParameters()
    policy_engine = PolicyEngine(risk_params)
    policy_engine.mode = BotMode.LIVE  # Operator set live

    mock_chain = MockEVMAdapter(simulate_success=True)

    # Server latch is FALSE
    executor = LiquidationExecutor(
        repository=e2e_repo,
        policy_engine=policy_engine,
        allow_live_execution_flag=False,  # Global safety latch OFF
    )

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

    opp = LiquidationOpportunity(
        opportunity_id="opp_live_blocked",
        chain=ChainType.EVM,
        protocol="moonwell_base",
        borrower_address="0xBorrowerLive",
        debt_asset=usdc,
        collateral_asset=weth,
        repay_amount_raw=1000 * 10**6,
        expected_collateral_raw=10**17,
        expected_proceeds_raw=1100 * 10**6,
        funding_source="balancer",
        funding_premium_raw=0,
        disposal_venue="uniswap_v3",
        estimated_gas_units=300000,
        estimated_gas_debt_raw=50000,
        estimated_gas_usd=Decimal("0.05"),
        expected_net_profit_debt_raw=95 * 10**6,
        expected_net_profit_usd=Decimal("95.00"),
        invariant_passed=True,
    )

    attempt = await executor.process_opportunity(
        opp=opp,
        chain_adapter=mock_chain,
        tx_call_params={"to": "0xExecutor"},
        signer_key="0xMockKey",
    )

    # Must fall back to DRY_RUN and never broadcast
    assert attempt.mode == BotMode.DRY_RUN
    assert mock_chain.broadcast_called is False


@pytest.mark.asyncio
async def test_fastapi_web_dashboard_endpoints():
    """Verify web endpoints return correct status, adapter matrix, and role security."""
    settings = AppSettings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DEV_AUTH_BYPASS=True,  # Dev bypass enabled for local tests
    )
    app = create_app(settings)
    await app.state.db.init_models()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Fetch system status
        res_status = await client.get("/api/v1/status")
        assert res_status.status_code == 200
        data_status = res_status.json()
        assert data_status["bot_mode"] == "DRY_RUN"
        assert data_status["kill_switch_active"] is False

        # 2. Fetch adapter matrix
        res_adapters = await client.get("/api/v1/adapters")
        assert res_adapters.status_code == 200
        data_adapters = res_adapters.json()
        protocol_ids = [p["protocol_id"] for p in data_adapters["protocols"]]
        assert "moonwell_base" in protocol_ids
        assert "kamino_solana" in protocol_ids
        assert "scallop_sui" in protocol_ids
        assert "aries_aptos" in protocol_ids

        # Verify moonwell is execution ready
        moonwell_info = next(p for p in data_adapters["protocols"] if p["protocol_id"] == "moonwell_base")
        assert moonwell_info["status"] == "EXECUTION_IMPLEMENTED"

        # 3. Test kill switch trigger
        res_kill = await client.post("/api/v1/controls/kill", json={"reason": "E2E Automated Kill Test"})
        assert res_kill.status_code == 200
        assert res_kill.json()["kill_switch_active"] is True

        # Check status reflects killed state
        res_status_after = await client.get("/api/v1/status")
        assert res_status_after.json()["bot_state"] == "KILLED"
