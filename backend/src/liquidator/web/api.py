"""FastAPI application routing for Liquidation Bot management dashboard."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import RedirectResponse, StreamingResponse
from liquidator.auth.oidc import GoogleOIDCClient, generate_pkce_pair, generate_state_and_nonce
from liquidator.auth.security import (
    AuthenticatedUser,
    SecurityManager,
)
from liquidator.core.config import AppSettings
from liquidator.core.models import (
    AdapterStatus,
    BotMode,
    BotState,
    ChainType,
    UserRole,
)
from liquidator.discovery.oracle_watcher import OracleUpdateWatcher
from liquidator.execution.executor import LiquidationExecutor
from liquidator.monitoring.metrics import EventHub
from liquidator.risk.policy_engine import PolicyEngine
from liquidator.storage.repository import Repository
from pydantic import BaseModel

# Transient OAuth state storage (in production, use redis or encrypted cookie)
_OAUTH_STATE_CACHE: Dict[str, Dict[str, str]] = {}


class ModeChangePayload(BaseModel):
    mode: BotMode
    confirmation: str = ""


class KillSwitchPayload(BaseModel):
    reason: str = "Manual Emergency Trigger"


def create_api_router(
    settings: AppSettings,
    repository: Repository,
    security: SecurityManager,
    policy_engine: PolicyEngine,
    executor: LiquidationExecutor,
    event_hub: EventHub,
    oracle_watcher: OracleUpdateWatcher,
    chain_adapters: Dict[ChainType, Any],
    protocol_adapters: Dict[str, Any],
    scanner: Optional[Any] = None,
) -> APIRouter:
    router = APIRouter()

    # --- Authentication Endpoints ---
    oidc_client = GoogleOIDCClient(settings)

    @router.get("/auth/login")
    async def auth_login():
        """Initiates Google OIDC authorization code flow with PKCE."""
        state, nonce = generate_state_and_nonce()
        pkce = generate_pkce_pair()
        _OAUTH_STATE_CACHE[state] = {
            "nonce": nonce,
            "verifier": pkce.code_verifier,
            "created_at": str(time.time()),
        }
        auth_url = oidc_client.get_authorization_url(
            state=state, nonce=nonce, code_challenge=pkce.code_challenge
        )
        return RedirectResponse(url=auth_url)

    @router.get("/auth/callback")
    async def auth_callback(
        request: Request,
        response: Response,
        code: Optional[str] = Query(None),
        state: Optional[str] = Query(None),
        error: Optional[str] = Query(None),
    ):
        """Google OIDC callback endpoint."""
        if error:
            raise HTTPException(status_code=400, detail=f"OAuth authorization error: {error}")
        if not code or not state:
            raise HTTPException(status_code=400, detail="Missing code or state parameter.")

        cached_state = _OAUTH_STATE_CACHE.pop(state, None)
        if not cached_state:
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state parameter.")

        try:
            tokens = await oidc_client.exchange_code_for_tokens(
                code=code, code_verifier=cached_state["verifier"]
            )
            user_info = await oidc_client.get_user_info(tokens["access_token"])
            is_authorized, role, err_msg = oidc_client.verify_and_authorize_identity(user_info)
            if not is_authorized or not role:
                raise HTTPException(status_code=403, detail=err_msg)

            # Create session
            await security.create_user_session(
                response=response,
                user_email=user_info["email"],
                user_sub=user_info["sub"],
                role=role,
            )

            await repository.log_audit(
                audit_id=f"audit_{uuid.uuid4().hex[:12]}",
                user_email=user_info["email"],
                user_sub=user_info["sub"],
                role=role,
                action="USER_LOGIN",
                details={"provider": "google_oidc"},
            )

            redirect_res = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
            # Re-attach cookies from response
            for header_val in response.headers.getlist("set-cookie"):
                redirect_res.headers.append("set-cookie", header_val)
            return redirect_res

        except Exception as e:
            raise HTTPException(status_code=400, detail=f"OIDC verification failed: {str(e)}")

    @router.post("/auth/dev-login")
    async def auth_dev_login(response: Response):
        """Mock offline authentication endpoint for local development/testing."""
        if not settings.DEV_AUTH_BYPASS:
            raise HTTPException(status_code=403, detail="DEV_AUTH_BYPASS is disabled.")
        if settings.ENVIRONMENT.lower() == "production":
            raise HTTPException(status_code=403, detail="Cannot bypass auth in production!")

        await security.create_user_session(
            response=response,
            user_email="dev-admin@example.com",
            user_sub="dev_admin_sub_01",
            role=UserRole.ADMINISTRATOR,
        )
        return {"status": "ok", "message": "Development mock session established"}

    @router.post("/auth/logout")
    async def auth_logout(
        request: Request,
        response: Response,
        user: AuthenticatedUser = Depends(security.get_current_user),
    ):
        await security.clear_session(request, response)
        await repository.log_audit(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            user_email=user.user_email,
            user_sub=user.user_sub,
            role=user.role,
            action="USER_LOGOUT",
            details={},
        )
        return {"status": "ok", "message": "Logged out successfully"}

    @router.get("/auth/me")
    async def auth_me(user: AuthenticatedUser = Depends(security.get_current_user)):
        return {
            "email": user.user_email,
            "role": user.role.value,
            "sub": user.user_sub,
            "last_auth_time": user.last_auth_time,
        }

    # --- System Status and Metrics ---
    @router.get("/api/v1/status")
    async def get_system_status(user: AuthenticatedUser = Depends(security.get_current_user)):
        # Chain health
        chain_health = {}
        for ctype, cadapter in chain_adapters.items():
            head = await cadapter.get_latest_block_number()
            chain_health[ctype.value] = {
                "head": head,
                "status": cadapter.status.value,
            }

        return {
            "bot_mode": policy_engine.mode.value,
            "bot_state": policy_engine.state.value,
            "kill_switch_active": policy_engine.kill_switch_active,
            "allow_live_execution": settings.ALLOW_LIVE_EXECUTION,
            "circuit_breaker_tripped": policy_engine.last_circuit_trip_reason is not None,
            "circuit_trip_reason": policy_engine.last_circuit_trip_reason,
            "consecutive_failures": policy_engine.consecutive_failures,
            "daily_realized_loss_usd": str(policy_engine.daily_realized_loss_usd),
            "chain_health": chain_health,
            "oracle_watcher": oracle_watcher.get_health_status(),
        }

    @router.get("/api/v1/adapters")
    async def get_adapters_matrix(user: AuthenticatedUser = Depends(security.get_current_user)):
        """Returns protocol and chain adapter capability statuses."""
        protocols_info = []
        for pid, padapter in protocol_adapters.items():
            protocols_info.append({
                "protocol_id": padapter.protocol_id,
                "chain": padapter.chain.value,
                "status": padapter.status.value,
                "is_execution_ready": padapter.status == AdapterStatus.EXECUTION_IMPLEMENTED,
            })

        chains_info = []
        for ctype, cadapter in chain_adapters.items():
            chains_info.append({
                "chain": cadapter.chain_type.value,
                "status": cadapter.status.value,
                "is_execution_ready": cadapter.status == AdapterStatus.EXECUTION_IMPLEMENTED,
            })

        return {
            "protocols": protocols_info,
            "chains": chains_info,
        }

    @router.get("/api/v1/metrics")
    async def get_metrics(user: AuthenticatedUser = Depends(security.get_current_user)):
        attempts = await repository.get_recent_attempts(limit=100)
        realized_pnl = sum(a.net_pnl_usd for a in attempts if a.status == "SUCCESS")
        gas_spent = sum(a.gas_cost_usd for a in attempts)

        return {
            "competition": event_hub.competition_metrics.model_dump(),
            "realized_pnl_usd": str(realized_pnl),
            "gas_costs_usd": str(gas_spent),
            "total_attempts": len(attempts),
            "successful_attempts": len([a for a in attempts if a.status == "SUCCESS"]),
            "failed_attempts": len([a for a in attempts if a.status in {"REVERTED", "SIMULATION_FAILED"}]),
        }

    # --- Positions & Opportunities ---
    @router.get("/api/v1/positions")
    async def get_positions(user: AuthenticatedUser = Depends(security.get_current_user)):
        positions = await repository.get_all_positions()
        return [
            {
                "id": p.id,
                "chain": p.chain,
                "protocol": p.protocol,
                "account_id": p.account_id,
                "debt_asset": p.debt_asset,
                "collateral_asset": p.collateral_asset,
                "debt_amount_raw": p.debt_amount_raw,
                "collateral_amount_raw": p.collateral_amount_raw,
                "health_factor": str(p.health_factor),
                "block_number": p.block_number,
                "last_updated": p.last_updated,
            }
            for p in positions
        ]

    @router.get("/api/v1/opportunities")
    async def get_opportunities(user: AuthenticatedUser = Depends(security.get_current_user)):
        opps = await repository.get_recent_opportunities(limit=50)
        return [
            {
                "opportunity_id": o.opportunity_id,
                "chain": o.chain,
                "protocol": o.protocol,
                "borrower_address": o.borrower_address,
                "debt_asset": o.debt_asset_address,
                "collateral_asset": o.collateral_asset_address,
                "repay_amount_raw": o.repay_amount_raw,
                "expected_collateral_raw": o.expected_collateral_raw,
                "expected_proceeds_raw": o.expected_proceeds_raw,
                "funding_source": o.funding_source,
                "disposal_venue": o.disposal_venue,
                "estimated_gas_usd": str(o.estimated_gas_usd),
                "expected_net_profit_usd": str(o.expected_net_profit_usd),
                "invariant_passed": o.invariant_passed,
                "rejection_reason": o.rejection_reason,
                "created_at": o.created_at,
            }
            for o in opps
        ]

    @router.get("/api/v1/rejections")
    async def get_rejection_logs(user: AuthenticatedUser = Depends(security.get_current_user)):
        logs = await repository.get_rejection_logs(limit=50)
        return [
            {
                "dedup_key": log_rec.dedup_key,
                "protocol": log_rec.protocol,
                "account_id": log_rec.account_id,
                "reason_code": log_rec.reason_code,
                "occurrence_count": log_rec.occurrence_count,
                "first_seen_at": log_rec.first_seen_at,
                "last_seen_at": log_rec.last_seen_at,
                "details": json.loads(log_rec.last_details_json),
            }
            for log_rec in logs
        ]

    # --- Attempts ---
    @router.get("/api/v1/attempts")
    async def get_attempts(user: AuthenticatedUser = Depends(security.get_current_user)):
        attempts = await repository.get_recent_attempts(limit=50)
        return [
            {
                "attempt_id": a.attempt_id,
                "opportunity_id": a.opportunity_id,
                "protocol": a.protocol,
                "account_id": a.account_id,
                "chain": a.chain,
                "mode": a.mode,
                "status": a.status,
                "tx_hash": a.tx_hash,
                "block_number": a.block_number,
                "repay_token": a.repay_token_symbol,
                "repay_amount_raw": a.repay_amount_raw,
                "proceeds_raw": a.proceeds_raw,
                "gas_units_used": a.gas_units_used,
                "gas_cost_usd": str(a.gas_cost_usd),
                "net_pnl_usd": str(a.net_pnl_usd),
                "failure_reason": a.failure_reason,
                "created_at": a.created_at,
                "finalized_at": a.finalized_at,
            }
            for a in attempts
        ]

    # --- Controls (Operator / Admin) ---
    @router.post("/api/v1/controls/start")
    async def control_start(
        user: AuthenticatedUser = Depends(security.require_role({UserRole.OPERATOR, UserRole.ADMINISTRATOR}))
    ):
        policy_engine.state = BotState.RUNNING
        await repository.log_audit(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            user_email=user.user_email,
            user_sub=user.user_sub,
            role=user.role,
            action="BOT_START",
            details={},
        )
        return {"status": "ok", "state": policy_engine.state.value}

    @router.post("/api/v1/controls/stop")
    async def control_stop(
        user: AuthenticatedUser = Depends(security.require_role({UserRole.OPERATOR, UserRole.ADMINISTRATOR}))
    ):
        policy_engine.state = BotState.STOPPED
        await repository.log_audit(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            user_email=user.user_email,
            user_sub=user.user_sub,
            role=user.role,
            action="BOT_STOP",
            details={},
        )
        return {"status": "ok", "state": policy_engine.state.value}

    @router.post("/api/v1/controls/pause")
    async def control_pause(
        user: AuthenticatedUser = Depends(security.require_role({UserRole.OPERATOR, UserRole.ADMINISTRATOR}))
    ):
        policy_engine.state = BotState.PAUSED
        await repository.log_audit(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            user_email=user.user_email,
            user_sub=user.user_sub,
            role=user.role,
            action="BOT_PAUSE",
            details={},
        )
        return {"status": "ok", "state": policy_engine.state.value}

    @router.post("/api/v1/controls/resume")
    async def control_resume(
        user: AuthenticatedUser = Depends(security.require_role({UserRole.OPERATOR, UserRole.ADMINISTRATOR}))
    ):
        policy_engine.reset_kill_switch()
        await repository.log_audit(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            user_email=user.user_email,
            user_sub=user.user_sub,
            role=user.role,
            action="BOT_RESUME",
            details={},
        )
        return {"status": "ok", "state": policy_engine.state.value}

    @router.post("/api/v1/controls/kill")
    async def control_kill(
        payload: KillSwitchPayload,
        user: AuthenticatedUser = Depends(security.require_role({UserRole.OPERATOR, UserRole.ADMINISTRATOR})),
    ):
        """Immediate Kill Switch."""
        policy_engine.trigger_kill_switch(reason=payload.reason)
        await repository.log_audit(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            user_email=user.user_email,
            user_sub=user.user_sub,
            role=user.role,
            action="KILL_SWITCH_TRIGGERED",
            details={"reason": payload.reason},
        )
        await event_hub.emit("kill_switch_triggered", {"reason": payload.reason})
        return {"status": "ok", "state": policy_engine.state.value, "kill_switch_active": True}

    @router.post("/api/v1/evaluate")
    async def trigger_evaluation(
        user: AuthenticatedUser = Depends(security.require_role({UserRole.OPERATOR, UserRole.ADMINISTRATOR})),
    ):
        """Manually trigger immediate evaluation and paper-trade simulation for eligible positions."""
        if not scanner:
            raise HTTPException(status_code=503, detail="Discovery scanner not initialized")
        results = await scanner.evaluate_eligible_positions(force=True)
        return {
            "status": "ok",
            "evaluated_count": len(results),
            "results": [
                {
                    "opportunity_id": r["opportunity"].opportunity_id,
                    "borrower": r["opportunity"].borrower_address,
                    "net_profit_usd": str(r["opportunity"].expected_net_profit_usd),
                    "invariant_passed": r["opportunity"].invariant_passed,
                    "attempt_status": r["attempt"].status if r["attempt"] else None,
                }
                for r in results
            ],
        }

    @router.post("/api/v1/controls/mode")
    async def control_set_mode(
        payload: ModeChangePayload,
        user: AuthenticatedUser = Depends(security.require_role({UserRole.ADMINISTRATOR})),
    ):
        """
        Sensitive action: Switch bot mode (DRY_RUN <-> LIVE).
        Requires typed confirmation 'CONFIRM_LIVE' to switch to live mode!
        """
        if payload.mode == BotMode.LIVE:
            if not settings.ALLOW_LIVE_EXECUTION:
                raise HTTPException(
                    status_code=403,
                    detail="Server configuration 'ALLOW_LIVE_EXECUTION' is false. Live mode is prohibited.",
                )
            security.verify_sensitive_action(
                user=user,
                typed_confirmation=payload.confirmation,
                expected_confirmation="CONFIRM_LIVE",
            )
            policy_engine.mode = BotMode.LIVE
        else:
            policy_engine.mode = BotMode.DRY_RUN

        await repository.log_audit(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            user_email=user.user_email,
            user_sub=user.user_sub,
            role=user.role,
            action="MODE_CHANGE",
            details={"new_mode": policy_engine.mode.value},
        )
        return {"status": "ok", "mode": policy_engine.mode.value}

    # --- Risk Settings ---
    @router.get("/api/v1/risk/config")
    async def get_risk_config(user: AuthenticatedUser = Depends(security.get_current_user)):
        return {
            "max_repay_per_liquidation_usd": str(policy_engine.params.max_repay_per_liquidation_usd),
            "min_expected_profit_usd": str(policy_engine.params.min_expected_profit_usd),
            "max_slippage_bps": policy_engine.params.max_slippage_bps,
            "max_daily_loss_usd": str(policy_engine.params.max_daily_loss_usd),
            "cooldown_seconds": policy_engine.params.cooldown_seconds,
            "allowlisted_protocols": policy_engine.params.allowlisted_protocols,
            "allowlisted_venues": policy_engine.params.allowlisted_venues,
        }

    # --- Audit Logs ---
    @router.get("/api/v1/audit/logs")
    async def get_audit_logs(user: AuthenticatedUser = Depends(security.require_role({UserRole.ADMINISTRATOR}))):
        logs = await repository.get_audit_logs(limit=50)
        return [
            {
                "id": audit_rec.id,
                "timestamp": audit_rec.timestamp,
                "user_email": audit_rec.user_email,
                "role": audit_rec.role,
                "action": audit_rec.action,
                "details": json.loads(audit_rec.details_json),
                "ip_address": audit_rec.ip_address,
                "success": audit_rec.success,
            }
            for audit_rec in logs
        ]

    # --- Server-Sent Events (SSE) Live Feed ---
    @router.get("/api/v1/stream")
    async def sse_event_stream(
        request: Request,
        user: AuthenticatedUser = Depends(security.get_current_user),
    ):
        """SSE stream broadcasting real-time liquidation attempts, circuit triggers, and blocks."""
        queue = event_hub.subscribe()

        async def event_generator():
            try:
                # Send initial ping
                yield f"event: connected\ndata: {json.dumps({'status': 'connected', 'timestamp': time.time()})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event_data = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"event: {event_data['type']}\ndata: {json.dumps(event_data)}\n\n"
                    except asyncio.TimeoutError:
                        # Send heartbeat comment to keep connection alive
                        yield ": heartbeat\n\n"
            finally:
                event_hub.unsubscribe(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
