"""Composition root and application factory for the Modular Liquidation Bot."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from liquidator.auth.security import SecurityManager
from liquidator.chains.aptos_adapter import AptosChainAdapter
from liquidator.chains.evm_adapter import EVMChainAdapter
from liquidator.chains.solana_adapter import SolanaChainAdapter
from liquidator.chains.sui_adapter import SuiChainAdapter
from liquidator.core.config import AppSettings
from liquidator.core.models import BorrowerPosition, ChainType, RiskParameters
from liquidator.discovery.inverted_index import InvertedIndexEngine
from liquidator.discovery.kamino_daemon import KaminoDiscoveryDaemon
from liquidator.discovery.oracle_watcher import OracleUpdateWatcher
from liquidator.discovery.scallop_daemon import ScallopDiscoveryDaemon
from liquidator.discovery.scanner import PositionDiscoveryScanner
from liquidator.eligibility.evaluator import OpportunityEvaluator
from liquidator.execution.executor import LiquidationExecutor
from liquidator.funding.balancer_flash_loan import BalancerFlashLoanAdapter
from liquidator.monitoring.metrics import EventHub
from liquidator.pricing.uniswap_v3_quoter import UniswapV3RouteAdapter
from liquidator.protocols.aries_adapter import AriesAdapter
from liquidator.protocols.kamino_adapter import KaminoAdapter
from liquidator.protocols.moonwell_adapter import MoonwellAdapter
from liquidator.protocols.scallop_adapter import ScallopAdapter
from liquidator.protocols.seamless_adapter import SeamlessAdapter
from liquidator.risk.policy_engine import PolicyEngine
from liquidator.storage.database import Database
from liquidator.storage.repository import Repository
from liquidator.web.api import create_api_router


def create_app(settings: AppSettings | None = None) -> FastAPI:
    if settings is None:
        settings = AppSettings()

    # 1. Persistence & Repository
    db = Database(settings.DATABASE_URL)
    repository = Repository(db.session_factory)

    # 2. Security Manager
    security = SecurityManager(settings, repository)

    # 3. Chain Adapters
    chain_adapters: Dict[ChainType, Any] = {
        ChainType.EVM: EVMChainAdapter(rpc_url=settings.BASE_RPC_URL),
        ChainType.SOLANA: SolanaChainAdapter(rpc_url=settings.SOLANA_RPC_URL),
        ChainType.SUI: SuiChainAdapter(rpc_url=settings.SUI_RPC_URL),
        ChainType.APTOS: AptosChainAdapter(rpc_url=settings.APTOS_RPC_URL),
    }

    # 4. Lending Protocol Adapters
    protocol_adapters: Dict[str, Any] = {
        "moonwell_base": MoonwellAdapter(comptroller_address=settings.MOONWELL_COMPTROLLER_ADDRESS),
        "seamless_base": SeamlessAdapter(),
        "kamino_solana": KaminoAdapter(),
        "scallop_sui": ScallopAdapter(),
        "aries_aptos": AriesAdapter(),
    }

    # 5. Funding & Route Adapters
    funding_adapter = BalancerFlashLoanAdapter(vault_address=settings.BALANCER_VAULT_ADDRESS)
    route_adapter = UniswapV3RouteAdapter(
        quoter_address=settings.UNISWAP_V3_QUOTER_ADDRESS,
        router_address=settings.UNISWAP_V3_ROUTER_ADDRESS,
    )

    # 6. Risk Policy Engine
    risk_params = RiskParameters(
        max_repay_per_liquidation_usd=settings.MAX_REPAY_USD,
        min_expected_profit_usd=settings.MIN_PROFIT_USD,
        max_slippage_bps=settings.MAX_SLIPPAGE_BPS,
        max_gas_price_gwei=settings.MAX_GAS_GWEI,
        max_daily_loss_usd=settings.MAX_DAILY_LOSS_USD,
        cooldown_seconds=settings.SIMULATION_COOLDOWN_SECONDS,
        max_oracle_staleness_seconds=settings.MAX_ORACLE_STALENESS_SECONDS,
    )
    policy_engine = PolicyEngine(risk_params)
    policy_engine.mode = settings.DEFAULT_BOT_MODE

    # 7. Event Sink & Metrics
    event_hub = EventHub()

    # 8. EVM Chain Adapter
    evm_adapter = EVMChainAdapter(rpc_url=settings.BASE_RPC_URL)

    # 9. Oracle Watcher (polling live Base Chainlink feeds)
    oracle_watcher = OracleUpdateWatcher(
        chain=ChainType.EVM,
        max_staleness_seconds=settings.MAX_ORACLE_STALENESS_SECONDS,
        w3=evm_adapter.w3,
    )

    # 10. Evaluator & Executor
    evaluator = OpportunityEvaluator(repository=repository, risk_params=risk_params)
    executor = LiquidationExecutor(
        repository=repository,
        policy_engine=policy_engine,
        allow_live_execution_flag=settings.ALLOW_LIVE_EXECUTION,
        event_sink=event_hub,
    )

    # 11. Execution Slice Adapters
    moonwell_adapter = MoonwellAdapter(
        comptroller_address=settings.MOONWELL_COMPTROLLER_ADDRESS,
        w3=evm_adapter.w3,
    )
    funding_adapter = BalancerFlashLoanAdapter(vault_address=settings.BALANCER_VAULT_ADDRESS)
    route_adapter = UniswapV3RouteAdapter(
        quoter_address=settings.UNISWAP_V3_QUOTER_ADDRESS,
        router_address=settings.UNISWAP_V3_ROUTER_ADDRESS,
        w3=evm_adapter.w3,
    )

    # 11. Inverted Index Engine (Zero-Latency Liquidation Thresholds & Hot/Cold Tiering)
    inverted_index = InvertedIndexEngine()

    # 12. Background Real-Time Block & Event Streaming Scanner
    scanner = PositionDiscoveryScanner(
        repository=repository,
        rpc_url=settings.BASE_RPC_URL,
        comptroller_address=settings.MOONWELL_COMPTROLLER_ADDRESS,
        poll_interval=2.0,  # Stream at 2s block intervals on Base
        event_sink=event_hub,
        evaluator=evaluator,
        executor=executor,
        chain_adapter=evm_adapter,
        protocol_adapter=moonwell_adapter,
        funding_adapter=funding_adapter,
        route_adapter=route_adapter,
        flash_liquidator_address=settings.FLASH_LIQUIDATOR_ADDRESS,
        inverted_index=inverted_index,
    )

    # 13. Solana & Sui Live Discovery Daemons
    kamino_daemon = KaminoDiscoveryDaemon(
        repository=repository,
        rpc_url=settings.SOLANA_RPC_URL,
        poll_interval=15.0,
        event_sink=event_hub,
        inverted_index=inverted_index,
    )
    scallop_daemon = ScallopDiscoveryDaemon(
        repository=repository,
        graphql_endpoint=settings.SUI_GRAPHQL_URL,
        poll_interval=20.0,
        event_sink=event_hub,
        inverted_index=inverted_index,
    )

    # Connect Oracle Watcher to Inverted Index for zero-latency price-drop triggering
    async def on_oracle_update(update):
        breached = inverted_index.update_price(update.asset_address, update.price_usd)
        for pos in breached:
            if scanner and pos.chain == ChainType.EVM:
                asyncio.create_task(scanner._evaluate_shortfall_position(pos, pos.block_number))

    oracle_watcher.on_price_update = on_oracle_update

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: initialize database tables
        await db.init_models()

        # Seed Inverted Index with persisted positions
        stored_positions = await repository.get_all_positions()
        for p in stored_positions:
            try:
                meta = {}
                if getattr(p, "metadata_json", None):
                    try:
                        meta = json.loads(p.metadata_json)
                    except Exception:
                        pass
                pos_obj = BorrowerPosition(
                    id=p.id,
                    chain=ChainType(p.chain),
                    protocol=p.protocol,
                    account_id=p.account_id,
                    debt_asset=p.debt_asset,
                    collateral_asset=p.collateral_asset,
                    debt_amount_raw=int(p.debt_amount_raw),
                    collateral_amount_raw=int(p.collateral_amount_raw),
                    health_factor=p.health_factor,
                    block_number=p.block_number,
                    last_updated=p.last_updated,
                    metadata=meta,
                )
                inverted_index.index_position(pos_obj)
            except Exception:
                pass

        scanner_task = asyncio.create_task(scanner.start())
        oracle_task = asyncio.create_task(oracle_watcher.start_polling_loop(poll_interval=10.0))
        kamino_task = asyncio.create_task(kamino_daemon.start())
        scallop_task = asyncio.create_task(scallop_daemon.start())
        yield
        # Shutdown: close DB & cancel background task
        scanner.stop()
        scanner_task.cancel()
        oracle_watcher.stop()
        oracle_task.cancel()
        kamino_daemon.stop()
        kamino_task.cancel()
        scallop_daemon.stop()
        scallop_task.cancel()
        await db.close()

    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        lifespan=lifespan,
    )

    # Templates directory
    template_dir = os.path.join(os.path.dirname(__file__), "web", "templates")
    templates = Jinja2Templates(directory=template_dir)

    # Attach router
    api_router = create_api_router(
        settings=settings,
        repository=repository,
        security=security,
        policy_engine=policy_engine,
        executor=executor,
        event_hub=event_hub,
        oracle_watcher=oracle_watcher,
        chain_adapters=chain_adapters,
        protocol_adapters=protocol_adapters,
        scanner=scanner,
        inverted_index=inverted_index,
    )
    app.include_router(api_router)

    # Web Dashboard UI Routes
    @app.get("/", response_class=HTMLResponse)
    async def render_dashboard(request: Request):
        try:
            # Check authentication
            user = await security.get_current_user(request)
        except Exception:
            return RedirectResponse(url="/login")

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "app_name": settings.APP_NAME,
                "allow_live_execution": settings.ALLOW_LIVE_EXECUTION,
                "user": user,
            },
        )

    @app.get("/login", response_class=HTMLResponse)
    async def render_login(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "app_name": settings.APP_NAME,
                "dev_auth_bypass": settings.DEV_AUTH_BYPASS,
            },
        )

    # Expose dependencies on app.state for testing & workers
    app.state.settings = settings
    app.state.db = db
    app.state.repository = repository
    app.state.security = security
    app.state.policy_engine = policy_engine
    app.state.evaluator = evaluator
    app.state.executor = executor
    app.state.event_hub = event_hub
    app.state.oracle_watcher = oracle_watcher
    app.state.chain_adapters = chain_adapters
    app.state.protocol_adapters = protocol_adapters
    app.state.funding_adapter = funding_adapter
    app.state.route_adapter = route_adapter
    app.state.kamino_daemon = kamino_daemon
    app.state.scallop_daemon = scallop_daemon
    app.state.inverted_index = inverted_index

    return app
