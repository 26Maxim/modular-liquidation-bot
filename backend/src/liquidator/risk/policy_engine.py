"""Risk Policy Engine and Circuit Breakers."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional, Tuple

from liquidator.core.models import (
    BotMode,
    BotState,
    LiquidationOpportunity,
    ReasonCode,
    RiskParameters,
)
from liquidator.core.scaling import raw_to_norm


class PolicyEngine:
    """
    Evaluates execution risk, enforces circuit breakers, notional caps, and allowlists.
    """

    def __init__(self, risk_params: RiskParameters):
        self.params = risk_params
        self.state: BotState = BotState.RUNNING
        self.mode: BotMode = BotMode.DRY_RUN
        self.kill_switch_active: bool = False

        # Operational counters
        self.consecutive_failures: int = 0
        self.daily_realized_loss_usd: Decimal = Decimal("0.0")
        self.last_circuit_trip_reason: Optional[str] = None
        self.circuit_tripped_at: Optional[float] = None

    def trigger_kill_switch(self, reason: str = "Operator Manual Trigger") -> None:
        """Immediately prevent any live executions and trip the circuit breaker."""
        self.kill_switch_active = True
        self.state = BotState.KILLED
        self.last_circuit_trip_reason = f"KILL SWITCH: {reason}"
        self.circuit_tripped_at = time.time()

    def reset_kill_switch(self) -> None:
        self.kill_switch_active = False
        self.state = BotState.RUNNING
        self.consecutive_failures = 0
        self.last_circuit_trip_reason = None

    def record_attempt_result(self, success: bool, net_pnl_usd: Decimal) -> None:
        if success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            if net_pnl_usd < Decimal("0.0"):
                self.daily_realized_loss_usd += abs(net_pnl_usd)

        # Check circuit breakers
        if self.consecutive_failures >= self.params.max_consecutive_failures:
            self.state = BotState.PAUSED
            self.last_circuit_trip_reason = (
                f"Tripped circuit breaker: {self.consecutive_failures} consecutive failures"
            )
            self.circuit_tripped_at = time.time()

        if self.daily_realized_loss_usd >= self.params.max_daily_loss_usd:
            self.state = BotState.PAUSED
            self.last_circuit_trip_reason = (
                f"Tripped circuit breaker: Daily loss (${self.daily_realized_loss_usd}) "
                f"exceeds limit (${self.params.max_daily_loss_usd})"
            )
            self.circuit_tripped_at = time.time()

    def authorize_execution(
        self,
        opp: LiquidationOpportunity,
    ) -> Tuple[bool, Optional[ReasonCode], str]:
        """
        Validates opportunity against all active risk policies and state constraints.
        Returns: (authorized, rejection_reason_code, detail_message)
        """
        # 1. Kill switch
        if self.kill_switch_active or self.state == BotState.KILLED:
            return False, ReasonCode.KILL_SWITCH_ACTIVE, "System kill switch is engaged."

        # 2. Bot State
        if self.state == BotState.PAUSED:
            return False, ReasonCode.UNKNOWN, f"Bot is PAUSED: {self.last_circuit_trip_reason}"
        if self.state == BotState.STOPPED:
            return False, ReasonCode.UNKNOWN, "Bot is STOPPED."

        # 3. Invariant check
        if not opp.invariant_passed:
            return False, opp.rejection_reason or ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE, "Reconciliation invariant failed."

        # 4. Maximum notional check
        repay_norm = raw_to_norm(opp.repay_amount_raw, opp.debt_asset.decimals)
        repay_usd = repay_norm * opp.debt_asset.price_usd
        if repay_usd > self.params.max_repay_per_liquidation_usd:
            return (
                False,
                ReasonCode.MAX_NOTIONAL_EXCEEDED,
                f"Repay notional (${repay_usd:.2f}) exceeds max allowed (${self.params.max_repay_per_liquidation_usd:.2f})",
            )

        # 5. Allowlist check for protocols
        if (
            self.params.allowlisted_protocols
            and opp.protocol not in self.params.allowlisted_protocols
        ):
            return (
                False,
                ReasonCode.ALLOWLIST_REJECTED,
                f"Protocol '{opp.protocol}' is not on the allowlist.",
            )

        # 6. Allowlist check for venues
        if (
            self.params.allowlisted_venues
            and opp.disposal_venue not in self.params.allowlisted_venues
        ):
            return (
                False,
                ReasonCode.ALLOWLIST_REJECTED,
                f"Venue '{opp.disposal_venue}' is not on the allowlist.",
            )

        # 7. Consecutive failures circuit check
        if self.consecutive_failures >= self.params.max_consecutive_failures:
            return (
                False,
                ReasonCode.UNKNOWN,
                f"Consecutive failure limit reached ({self.consecutive_failures}).",
            )

        return True, None, "Authorized by policy engine."
