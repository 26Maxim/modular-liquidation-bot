"""Liquidation Opportunity Evaluator and Eligibility Engine."""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

from liquidator.core.interfaces import (
    CollateralRouteAdapter,
    FundingAdapter,
    LendingProtocolAdapter,
)
from liquidator.core.models import (
    BorrowerPosition,
    LiquidationOpportunity,
    ReasonCode,
    RiskParameters,
    Token,
)
from liquidator.core.scaling import (
    native_gas_to_debt_units,
    norm_to_raw,
    verify_reconciliation_invariant,
)
from liquidator.storage.repository import Repository


class OpportunityEvaluator:
    """
    Evaluates borrower positions against protocol liquidation rules,
    queries DEX quotes, checks invariants, and enforces simulation cooldowns.
    """

    def __init__(
        self,
        repository: Repository,
        risk_params: RiskParameters,
    ):
        self.repository = repository
        self.risk_params = risk_params

    async def evaluate_position(
        self,
        position: BorrowerPosition,
        debt_token: Token,
        collateral_token: Token,
        native_token_price_usd: Decimal,
        protocol_adapter: LendingProtocolAdapter,
        funding_adapter: FundingAdapter,
        route_adapter: CollateralRouteAdapter,
        estimated_gas_units: int = 350_000,
        detection_block: Optional[int] = None,
    ) -> LiquidationOpportunity:
        opp_id = f"opp_{uuid.uuid4().hex[:12]}"
        now = time.time()

        # 1. Eligibility Check: Is the borrower position liquidatable?
        if position.health_factor >= Decimal("1.0"):
            return self._build_ineligible_opp(
                opp_id=opp_id,
                position=position,
                debt_token=debt_token,
                collateral_token=collateral_token,
                reason=ReasonCode.HEALTH_FACTOR_HEALTHY,
            )

        # 2. Check Cooldown & State-Change Guard
        # Reserves hash summary (for DEX pool and debt/collateral state)
        reserves_hash = f"{debt_token.price_usd}:{collateral_token.price_usd}"
        in_cooldown = await self.repository.is_in_cooldown(
            protocol=position.protocol,
            account_id=position.account_id,
            debt_asset=position.debt_asset,
            collateral_asset=position.collateral_asset,
            current_oracle_price=collateral_token.price_usd,
            current_reserves_hash=reserves_hash,
            cooldown_duration=self.risk_params.cooldown_seconds,
        )
        if in_cooldown:
            return self._build_ineligible_opp(
                opp_id=opp_id,
                position=position,
                debt_token=debt_token,
                collateral_token=collateral_token,
                reason=ReasonCode.POSITION_COOLDOWN,
            )

        # 3. Calculate Allowable Repay and Seized Collateral using Protocol Rules
        try:
            repay_raw, expected_collateral_raw = protocol_adapter.calculate_allowable_repay_and_collateral(
                position=position,
                debt_token=debt_token,
                collateral_token=collateral_token,
            )
        except Exception:
            return self._build_ineligible_opp(
                opp_id=opp_id,
                position=position,
                debt_token=debt_token,
                collateral_token=collateral_token,
                reason=ReasonCode.UNKNOWN,
            )

        if repay_raw <= 0 or expected_collateral_raw <= 0:
            return self._build_ineligible_opp(
                opp_id=opp_id,
                position=position,
                debt_token=debt_token,
                collateral_token=collateral_token,
                reason=ReasonCode.INSUFFICIENT_COLLATERAL,
            )

        # 4. Check Funding Provider Liquidity and Premium
        available_liquidity = await funding_adapter.get_available_liquidity(debt_token)
        if available_liquidity < repay_raw:
            return self._build_ineligible_opp(
                opp_id=opp_id,
                position=position,
                debt_token=debt_token,
                collateral_token=collateral_token,
                reason=ReasonCode.INSUFFICIENT_FLASH_LIQUIDITY,
            )
        flash_premium_raw = funding_adapter.calculate_fee(debt_token, repay_raw)

        # 5. Query Collateral Disposal Route for Exact Sized Proceeds at detection block
        target_block = detection_block or position.block_number
        try:
            proceeds_raw = await route_adapter.get_quote(
                from_token=collateral_token,
                to_token=debt_token,
                amount_in_raw=expected_collateral_raw,
                block_identifier=target_block,
            )
        except Exception as e:
            # Fail closed: If static call reverts or can't run, record no opportunity
            logger.warning(
                f"Fail closed: QuoterV2 quote failed for {position.account_id} at block {target_block}: {e}"
            )
            return self._build_ineligible_opp(
                opp_id=opp_id,
                position=position,
                debt_token=debt_token,
                collateral_token=collateral_token,
                reason=ReasonCode.SLIPPAGE_EXCEEDED,
            )

        # 6. Convert Gas to Debt Asset Units and USD (Mandatory Section 3.5)
        gas_in_debt_raw, gas_cost_usd = native_gas_to_debt_units(
            gas_native_raw=estimated_gas_units * 10**9,  # e.g., 350k units * 1 gwei
            chain=position.chain,
            native_price_usd=native_token_price_usd,
            debt_token=debt_token,
            max_price_staleness_sec=self.risk_params.max_oracle_staleness_seconds,
        )

        # Minimum profit in debt asset units
        min_profit_norm = self.risk_params.min_expected_profit_usd / debt_token.price_usd
        min_profit_in_debt_raw = norm_to_raw(min_profit_norm, debt_token.decimals)

        # 7. Check Invariant Gates
        inv_result = verify_reconciliation_invariant(
            collateral_sold_proceeds_raw=proceeds_raw,
            debt_to_repay_raw=repay_raw,
            flash_premium_raw=flash_premium_raw,
            gas_in_debt_raw=gas_in_debt_raw,
            min_profit_in_debt_raw=min_profit_in_debt_raw,
            debt_token=debt_token,
        )

        if not inv_result.passed:
            # Place in cooldown to avoid RPC exhaustion
            await self.repository.set_cooldown(
                protocol=position.protocol,
                account_id=position.account_id,
                debt_asset=position.debt_asset,
                collateral_asset=position.collateral_asset,
                oracle_price=collateral_token.price_usd,
                reserves_hash=reserves_hash,
                reason_code=inv_result.rejection_reason or ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE,
            )
            # Deduplicated rejection log (no volatile gas values in dedup key)
            await self.repository.log_rejection(
                protocol=position.protocol,
                account_id=position.account_id,
                reason_code=inv_result.rejection_reason or ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE,
                details={
                    "gas_usd": str(gas_cost_usd),
                    "repay_raw": str(repay_raw),
                    "proceeds_raw": str(proceeds_raw),
                    "net_profit_usd": str(inv_result.net_profit_usd),
                    "detail": inv_result.detail,
                },
            )

        opp = LiquidationOpportunity(
            opportunity_id=opp_id,
            chain=position.chain,
            protocol=position.protocol,
            borrower_address=position.account_id,
            debt_asset=debt_token,
            collateral_asset=collateral_token,
            repay_amount_raw=repay_raw,
            expected_collateral_raw=expected_collateral_raw,
            expected_proceeds_raw=proceeds_raw,
            funding_source=funding_adapter.name,
            funding_premium_raw=flash_premium_raw,
            disposal_venue=route_adapter.venue_name,
            estimated_gas_units=estimated_gas_units,
            estimated_gas_debt_raw=gas_in_debt_raw,
            estimated_gas_usd=gas_cost_usd,
            expected_net_profit_debt_raw=inv_result.net_proceeds_raw,
            expected_net_profit_usd=inv_result.net_profit_usd,
            invariant_passed=inv_result.passed,
            rejection_reason=inv_result.rejection_reason,
            detection_block=target_block,
            created_at=now,
        )

        await self.repository.save_opportunity(opp)
        return opp

    def _build_ineligible_opp(
        self,
        opp_id: str,
        position: BorrowerPosition,
        debt_token: Token,
        collateral_token: Token,
        reason: ReasonCode,
    ) -> LiquidationOpportunity:
        return LiquidationOpportunity(
            opportunity_id=opp_id,
            chain=position.chain,
            protocol=position.protocol,
            borrower_address=position.account_id,
            debt_asset=debt_token,
            collateral_asset=collateral_token,
            repay_amount_raw=0,
            expected_collateral_raw=0,
            expected_proceeds_raw=0,
            funding_source="none",
            funding_premium_raw=0,
            disposal_venue="none",
            estimated_gas_units=0,
            estimated_gas_debt_raw=0,
            estimated_gas_usd=Decimal("0.0"),
            expected_net_profit_debt_raw=0,
            expected_net_profit_usd=Decimal("0.0"),
            invariant_passed=False,
            rejection_reason=reason,
        )
