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
from liquidator.discovery.dynamic_scanner import DynamicMultiChainScanner
from liquidator.discovery.inverted_index import InvertedIndexEngine
from liquidator.discovery.kamino_daemon import KaminoDiscoveryDaemon
from liquidator.discovery.oracle_watcher import OracleUpdateWatcher
from liquidator.discovery.scallop_daemon import ScallopDiscoveryDaemon
from liquidator.discovery.scanner import PositionDiscoveryScanner
from liquidator.eligibility.evaluator import OpportunityEvaluator
from liquidator.execution.executor import LiquidationExecutor
from liquidator.funding.balancer_flash_loan import BalancerFlashLoanAdapter
from liquidator.funding.self_funded import SelfFundedAdapter
from liquidator.monitoring.metrics import EventHub
from liquidator.pricing.uniswap_v3_quoter import UniswapV3RouteAdapter
from liquidator.protocols.aries_adapter import AriesAdapter
from liquidator.protocols.generic_lending_adapter import GenericLendingAdapter
from liquidator.protocols.kamino_adapter import KaminoAdapter
from liquidator.protocols.moonwell_adapter import MoonwellAdapter
from liquidator.protocols.scallop_adapter import ScallopAdapter
from liquidator.protocols.seamless_adapter import SeamlessAdapter
from liquidator.risk.policy_engine import PolicyEngine
from liquidator.storage.database import Database
from liquidator.storage.models import ChainRegistryRecord
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

    # 12. Self-Funded Execution Adapter (0 bps fee, low competition on new chains)
    self_funded_adapter = SelfFundedAdapter()

    # 13. Dynamic Multi-Chain Discovery Scanner (Discovers markets on new EVM chains)
    dynamic_scanner = DynamicMultiChainScanner(
        repository=repository,
        inverted_index=inverted_index,
        evaluator=evaluator,
        executor=executor,
        funding_adapter=self_funded_adapter,
        route_adapter=route_adapter,
        chain_adapter=evm_adapter,
        event_sink=event_hub,
        poll_interval=5.0,
    )

    # Legacy daemons kept in app.state for backward compatibility
    scanner = PositionDiscoveryScanner(
        repository=repository,
        rpc_url=settings.BASE_RPC_URL,
        comptroller_address=settings.MOONWELL_COMPTROLLER_ADDRESS,
        poll_interval=10.0,
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
    kamino_daemon = KaminoDiscoveryDaemon(
        repository=repository,
        rpc_url=settings.SOLANA_RPC_URL,
        poll_interval=60.0,
        event_sink=event_hub,
        inverted_index=inverted_index,
    )
    scallop_daemon = ScallopDiscoveryDaemon(
        repository=repository,
        graphql_endpoint=settings.SUI_GRAPHQL_URL,
        poll_interval=60.0,
        event_sink=event_hub,
        inverted_index=inverted_index,
    )

    # Connect Oracle Watcher to Inverted Index for zero-latency price-drop triggering
    async def on_oracle_update(update):
        breached = inverted_index.update_price(update.asset_address, update.price_usd)
        for pos in breached:
            if dynamic_scanner and pos.chain == ChainType.EVM:
                asyncio.create_task(dynamic_scanner.evaluate_eligible_positions(force=True))

    oracle_watcher.on_price_update = on_oracle_update

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: initialize database tables
        await db.init_models()

        # Purge legacy red-ocean data from repository and inverted index
        await repository.purge_red_ocean_data()
        inverted_index.remove_protocols({"moonwell_base", "kamino_solana", "scallop_sui", "seamless_base"})

        # Seed initial Blue Ocean chains if registry has fewer than 6
        default_chains = [
            {
                "chain_id": "linea",
                "chain_name": "Linea zkEVM",
                "chain_type": "EVM",
                "rpc_url": "https://rpc.linea.build",
                "protocol_type": "aave_v3",
                "protocol_name": "ZeroLend",
                "comptroller_or_pool_address": "0x2f9bB73a8e98793e26Cb2F6C4ad037BDf1C6B269",
                "dex_router_address": "0x323e20e8b5a0364e037c87c71d6046e7f8272995",
                "funding_mode": "SELF_FUNDED",
                "is_active": True,
            },
            {
                "chain_id": "mantle",
                "chain_name": "Mantle Network",
                "chain_type": "EVM",
                "rpc_url": "https://rpc.mantle.xyz",
                "protocol_type": "aave_v3",
                "protocol_name": "Lendle",
                "comptroller_or_pool_address": "0xCFa5aE7c2CE8Fadc6426C1ff872cA45378Fb7cF3",
                "dex_router_address": "0x13374200c29C757FDCc72F15Da98fb94f286d71e",
                "funding_mode": "SELF_FUNDED",
                "is_active": True,
            },
            {
                "chain_id": "sonic",
                "chain_name": "Sonic Mainnet",
                "chain_type": "EVM",
                "rpc_url": "https://rpc.soniclabs.com",
                "protocol_type": "compound_v2",
                "protocol_name": "Rings Lending",
                "comptroller_or_pool_address": "0x5C808E56B486c478a89F2A6F14Fd5c2196627C44",
                "dex_router_address": "0x3b664d4b295f70a169b2075592182c1630b42c65",
                "funding_mode": "SELF_FUNDED",
                "is_active": True,
            },
            {
                "chain_id": "scroll",
                "chain_name": "Scroll zkEVM",
                "chain_type": "EVM",
                "rpc_url": "https://rpc.scroll.io",
                "protocol_type": "aave_v3",
                "protocol_name": "Aave v3 Scroll",
                "comptroller_or_pool_address": "0x69850D0B276776781C063771b161bd8894BCdD04",
                "dex_router_address": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
                "funding_mode": "SELF_FUNDED",
                "is_active": True,
            },
            {
                "chain_id": "mode",
                "chain_name": "Mode Network",
                "chain_type": "EVM",
                "rpc_url": "https://mainnet.mode.network",
                "protocol_type": "compound_v2",
                "protocol_name": "Ionic Protocol",
                "comptroller_or_pool_address": "0x3d30F326D13A0d309A71158c381c81F19bC16259",
                "dex_router_address": "0x5D61c537393cf21893BE619E36fC94cd73C77DD3",
                "funding_mode": "SELF_FUNDED",
                "is_active": True,
            },
            {
                "chain_id": "taiko",
                "chain_name": "Taiko Mainnet",
                "chain_type": "EVM",
                "rpc_url": "https://rpc.mainnet.taiko.xyz",
                "protocol_type": "compound_v2",
                "protocol_name": "Taiko Lending",
                "comptroller_or_pool_address": "0x13374200c29C757FDCc72F15Da98fb94f286d71e",
                "dex_router_address": "0x27239549DD40e1d60F5B80B0C4196923745B1FD2",
                "funding_mode": "SELF_FUNDED",
                "is_active": True,
            },
        ]
        for c_data in default_chains:
            await repository.upsert_registered_chain(c_data)

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

        dynamic_task = asyncio.create_task(dynamic_scanner.start())
        oracle_task = asyncio.create_task(oracle_watcher.start_polling_loop(poll_interval=15.0))
        yield
        # Shutdown: close DB & cancel background task
        dynamic_scanner.stop()
        dynamic_task.cancel()
        oracle_watcher.stop()
        oracle_task.cancel()
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
        scanner=dynamic_scanner,
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
    app.state.dynamic_scanner = dynamic_scanner
    app.state.self_funded_adapter = self_funded_adapter

    return app
