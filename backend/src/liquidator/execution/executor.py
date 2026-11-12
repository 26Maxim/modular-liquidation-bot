"""Execution orchestrator managing simulation, transaction submission, and reconciliation."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from liquidator.core.interfaces import ChainAdapter, EventSink
from liquidator.core.models import (
    AttemptOutcome,
    BotMode,
    LiquidationAttempt,
    LiquidationOpportunity,
)
from liquidator.risk.policy_engine import PolicyEngine
from liquidator.storage.repository import Repository


class LiquidationExecutor:
    """
    Executes or dry-runs liquidations following strict security gates and invariants.
    """

    def __init__(
        self,
        repository: Repository,
        policy_engine: PolicyEngine,
        allow_live_execution_flag: bool,
        event_sink: Optional[EventSink] = None,
    ):
        self.repository = repository
        self.policy_engine = policy_engine
        self.allow_live_execution_flag = allow_live_execution_flag
        self.event_sink = event_sink

    async def process_opportunity(
        self,
        opp: LiquidationOpportunity,
        chain_adapter: ChainAdapter,
        tx_call_params: Dict[str, Any],
        signer_key: Optional[str] = None,
    ) -> LiquidationAttempt:
        attempt_id = f"att_{uuid.uuid4().hex[:12]}"
        now = time.time()
        mode = self.policy_engine.mode

        # 1. Policy authorization
        authorized, rejection_code, policy_msg = self.policy_engine.authorize_execution(opp)
        if not authorized:
            attempt = LiquidationAttempt(
                attempt_id=attempt_id,
                opportunity_id=opp.opportunity_id,
                protocol=opp.protocol,
                account_id=opp.borrower_address,
                chain=opp.chain,
                mode=mode,
                status=AttemptOutcome.REJECTED_BY_POLICY,
                detection_block=opp.detection_block,
                repay_token_symbol=opp.debt_asset.symbol,
                repay_amount_raw=opp.repay_amount_raw,
                failure_reason=f"{rejection_code.value if rejection_code else 'REJECTED'}: {policy_msg}",
                created_at=now,
                finalized_at=now,
            )
            await self.repository.record_attempt(attempt)
            return attempt

        # 2. Simulate transaction on-chain
        sim_result = await chain_adapter.simulate_transaction(tx_call_params)
        if not sim_result.success:
            self.policy_engine.record_attempt_result(success=False, net_pnl_usd=Decimal("0.0"))
            attempt = LiquidationAttempt(
                attempt_id=attempt_id,
                opportunity_id=opp.opportunity_id,
                protocol=opp.protocol,
                account_id=opp.borrower_address,
                chain=opp.chain,
                mode=mode,
                status=AttemptOutcome.SIMULATION_FAILED,
                detection_block=opp.detection_block,
                repay_token_symbol=opp.debt_asset.symbol,
                repay_amount_raw=opp.repay_amount_raw,
                gas_units_used=sim_result.gas_used,
                failure_reason=sim_result.error_message or "Simulation reverted on-chain",
                created_at=now,
                finalized_at=now,
            )
            await self.repository.record_attempt(attempt)
            return attempt

        # 3. Mode Branch: DRY_RUN vs LIVE
        if mode == BotMode.DRY_RUN or not self.allow_live_execution_flag:
            # DRY RUN: Never broadcast transactions!
            attempt = LiquidationAttempt(
                attempt_id=attempt_id,
                opportunity_id=opp.opportunity_id,
                protocol=opp.protocol,
                account_id=opp.borrower_address,
                chain=opp.chain,
                mode=BotMode.DRY_RUN,
                status=AttemptOutcome.SUCCESS,
                tx_hash=None,
                detection_block=opp.detection_block,
                repay_token_symbol=opp.debt_asset.symbol,
                repay_amount_raw=opp.repay_amount_raw,
                proceeds_raw=opp.expected_proceeds_raw,
                gas_units_used=sim_result.gas_used,
                gas_cost_usd=opp.estimated_gas_usd,
                net_pnl_usd=opp.expected_net_profit_usd,
                failure_reason=None,
                created_at=now,
                finalized_at=now,
            )
            await self.repository.record_attempt(attempt)
            if self.event_sink:
                await self.event_sink.emit("dry_run_attempt_completed", attempt.model_dump())
            return attempt

        # 4. LIVE Mode Execution (Strict verification)
        if not signer_key:
            attempt = LiquidationAttempt(
                attempt_id=attempt_id,
                opportunity_id=opp.opportunity_id,
                protocol=opp.protocol,
                account_id=opp.borrower_address,
                chain=opp.chain,
                mode=BotMode.LIVE,
                status=AttemptOutcome.REJECTED_BY_POLICY,
                detection_block=opp.detection_block,
                repay_token_symbol=opp.debt_asset.symbol,
                repay_amount_raw=opp.repay_amount_raw,
                failure_reason="Live signing failed: No private key or signing provider configured.",
                created_at=now,
                finalized_at=now,
            )
            await self.repository.record_attempt(attempt)
            return attempt

        try:
            # Broadcast transaction
            tx_hash = await chain_adapter.submit_transaction(tx_call_params)

            # Record pending submission
            attempt = LiquidationAttempt(
                attempt_id=attempt_id,
                opportunity_id=opp.opportunity_id,
                protocol=opp.protocol,
                account_id=opp.borrower_address,
                chain=opp.chain,
                mode=BotMode.LIVE,
                status=AttemptOutcome.PENDING,
                tx_hash=tx_hash,
                repay_token_symbol=opp.debt_asset.symbol,
                repay_amount_raw=opp.repay_amount_raw,
                gas_units_used=sim_result.gas_used,
                gas_cost_usd=opp.estimated_gas_usd,
                net_pnl_usd=opp.expected_net_profit_usd,
                created_at=now,
            )
            await self.repository.record_attempt(attempt)

            # Reconcile inclusion status
            outcome, receipt_meta = await chain_adapter.get_transaction_status(tx_hash)
            attempt.status = outcome
            attempt.finalized_at = time.time()
            if outcome == AttemptOutcome.SUCCESS:
                self.policy_engine.record_attempt_result(
                    success=True, net_pnl_usd=opp.expected_net_profit_usd
                )
            else:
                self.policy_engine.record_attempt_result(
                    success=False, net_pnl_usd=-opp.estimated_gas_usd
                )

            await self.repository.record_attempt(attempt)
            if self.event_sink:
                await self.event_sink.emit("live_attempt_finalized", attempt.model_dump())
            return attempt

        except Exception as e:
            self.policy_engine.record_attempt_result(
                success=False, net_pnl_usd=-opp.estimated_gas_usd
            )
            attempt = LiquidationAttempt(
                attempt_id=attempt_id,
                opportunity_id=opp.opportunity_id,
                protocol=opp.protocol,
                account_id=opp.borrower_address,
                chain=opp.chain,
                mode=BotMode.LIVE,
                status=AttemptOutcome.REVERTED,
                repay_token_symbol=opp.debt_asset.symbol,
                repay_amount_raw=opp.repay_amount_raw,
                gas_cost_usd=opp.estimated_gas_usd,
                net_pnl_usd=-opp.estimated_gas_usd,
                failure_reason=f"Broadcast error: {e}",
                created_at=now,
                finalized_at=time.time(),
            )
            await self.repository.record_attempt(attempt)
            return attempt
