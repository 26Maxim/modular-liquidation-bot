"""Repository layer for database operations."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from liquidator.core.models import (
    BorrowerPosition,
    LiquidationAttempt,
    LiquidationOpportunity,
    ReasonCode,
    UserRole,
)
from liquidator.storage.models import (
    AttemptRecord,
    AuditLogRecord,
    BorrowerPositionRecord,
    OpportunityRecord,
    RejectionLogRecord,
    SimulationCooldownRecord,
    UserSessionRecord,
)
from sqlalchemy import select


class Repository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def upsert_borrower_position(self, pos: BorrowerPosition) -> None:
        pos_id = f"{pos.chain.value}_{pos.protocol}_{pos.account_id}"
        meta_str = json.dumps(pos.metadata, default=str) if pos.metadata else None
        async with self.session_factory() as session:
            record = await session.get(BorrowerPositionRecord, pos_id)
            if not record:
                record = BorrowerPositionRecord(
                    id=pos_id,
                    chain=pos.chain.value,
                    protocol=pos.protocol,
                    account_id=pos.account_id,
                    debt_asset=pos.debt_asset,
                    collateral_asset=pos.collateral_asset,
                    debt_amount_raw=str(pos.debt_amount_raw),
                    collateral_amount_raw=str(pos.collateral_amount_raw),
                    health_factor=pos.health_factor,
                    block_number=pos.block_number,
                    last_updated=pos.last_updated,
                    liquidation_price_usd=pos.liquidation_price_usd,
                    tier=pos.tier or "COLD",
                    collateral_symbol=pos.collateral_symbol,
                    debt_symbol=pos.debt_symbol,
                    distance_to_liq_pct=pos.distance_to_liq_pct,
                    metadata_json=meta_str,
                )
                session.add(record)
            else:
                record.debt_asset = pos.debt_asset
                record.collateral_asset = pos.collateral_asset
                record.debt_amount_raw = str(pos.debt_amount_raw)
                record.collateral_amount_raw = str(pos.collateral_amount_raw)
                record.health_factor = pos.health_factor
                record.block_number = pos.block_number
                record.last_updated = pos.last_updated
                record.liquidation_price_usd = pos.liquidation_price_usd
                record.tier = pos.tier or "COLD"
                record.collateral_symbol = pos.collateral_symbol
                record.debt_symbol = pos.debt_symbol
                record.distance_to_liq_pct = pos.distance_to_liq_pct
                record.metadata_json = meta_str
            await session.commit()

    async def get_all_positions(self) -> List[BorrowerPositionRecord]:
        async with self.session_factory() as session:
            res = await session.execute(
                select(BorrowerPositionRecord).order_by(BorrowerPositionRecord.health_factor.asc())
            )
            return list(res.scalars().all())

    async def get_positions_by_asset(self, asset_address: str) -> List[BorrowerPositionRecord]:
        async with self.session_factory() as session:
            addr_lower = asset_address.lower()
            res = await session.execute(
                select(BorrowerPositionRecord).where(
                    (BorrowerPositionRecord.debt_asset.ilike(addr_lower))
                    | (BorrowerPositionRecord.collateral_asset.ilike(addr_lower))
                )
            )
            return list(res.scalars().all())

    # --- Simulation Cooldown & State-Change Guard ---
    async def is_in_cooldown(
        self,
        protocol: str,
        account_id: str,
        debt_asset: str,
        collateral_asset: str,
        current_oracle_price: Decimal,
        current_reserves_hash: str,
        cooldown_duration: int = 60,
    ) -> bool:
        """
        Check if (protocol, account_id, debt_asset, collateral_asset) is currently in cooldown.
        Bypass cooldown if oracle price OR reserves hash changed since failure!
        """
        key = f"{protocol}:{account_id}:{debt_asset}:{collateral_asset}"
        now = time.time()
        async with self.session_factory() as session:
            record = await session.get(SimulationCooldownRecord, key)
            if not record:
                return False

            # Check elapsed time
            if now - record.last_failed_at >= cooldown_duration:
                # Cooldown expired
                return False

            # Check state-change guard: has price or reserve state changed?
            price_changed = abs(Decimal(record.oracle_price_at_failure) - current_oracle_price) > Decimal("0.0001")
            reserves_changed = record.reserves_hash_at_failure != current_reserves_hash

            if price_changed or reserves_changed:
                # State changed, invalidates cooldown!
                return False

            # State unchanged and time < cooldown_duration -> in cooldown
            return True

    async def set_cooldown(
        self,
        protocol: str,
        account_id: str,
        debt_asset: str,
        collateral_asset: str,
        oracle_price: Decimal,
        reserves_hash: str,
        reason_code: ReasonCode,
    ) -> None:
        key = f"{protocol}:{account_id}:{debt_asset}:{collateral_asset}"
        now = time.time()
        async with self.session_factory() as session:
            record = await session.get(SimulationCooldownRecord, key)
            if not record:
                record = SimulationCooldownRecord(
                    cooldown_key=key,
                    protocol=protocol,
                    account_id=account_id,
                    debt_asset=debt_asset,
                    collateral_asset=collateral_asset,
                    last_failed_at=now,
                    oracle_price_at_failure=oracle_price,
                    reserves_hash_at_failure=reserves_hash,
                    reason_code=reason_code.value,
                )
                session.add(record)
            else:
                record.last_failed_at = now
                record.oracle_price_at_failure = oracle_price
                record.reserves_hash_at_failure = reserves_hash
                record.reason_code = reason_code.value
            await session.commit()

    # --- Rejection Logging (Deduplicated on (protocol, account_id, reason_code)) ---
    async def log_rejection(
        self,
        protocol: str,
        account_id: str,
        reason_code: ReasonCode,
        details: Dict[str, Any],
    ) -> None:
        """
        Deduplicates on (protocol, account_id, reason_code).
        Volatile metrics (gas prices, wei amounts) stored in details_json only.
        """
        dedup_key = f"{protocol}:{account_id}:{reason_code.value}"
        now = time.time()
        details_str = json.dumps(details)

        async with self.session_factory() as session:
            record = await session.get(RejectionLogRecord, dedup_key)
            if not record:
                record = RejectionLogRecord(
                    dedup_key=dedup_key,
                    protocol=protocol,
                    account_id=account_id,
                    reason_code=reason_code.value,
                    occurrence_count=1,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_details_json=details_str,
                )
                session.add(record)
            else:
                record.occurrence_count += 1
                record.last_seen_at = now
                record.last_details_json = details_str
            await session.commit()

    async def get_rejection_logs(self, limit: int = 50) -> List[RejectionLogRecord]:
        async with self.session_factory() as session:
            res = await session.execute(
                select(RejectionLogRecord).order_by(RejectionLogRecord.last_seen_at.desc()).limit(limit)
            )
            return list(res.scalars().all())

    # --- Opportunities ---
    async def save_opportunity(self, opp: LiquidationOpportunity) -> None:
        async with self.session_factory() as session:
            record = OpportunityRecord(
                opportunity_id=opp.opportunity_id,
                chain=opp.chain.value,
                protocol=opp.protocol,
                borrower_address=opp.borrower_address,
                debt_asset_address=opp.debt_asset.address,
                collateral_asset_address=opp.collateral_asset.address,
                repay_amount_raw=str(opp.repay_amount_raw),
                expected_collateral_raw=str(opp.expected_collateral_raw),
                expected_proceeds_raw=str(opp.expected_proceeds_raw),
                funding_source=opp.funding_source,
                disposal_venue=opp.disposal_venue,
                estimated_gas_usd=opp.estimated_gas_usd,
                expected_net_profit_usd=opp.expected_net_profit_usd,
                invariant_passed=opp.invariant_passed,
                rejection_reason=opp.rejection_reason.value if opp.rejection_reason else None,
                detection_block=opp.detection_block,
                created_at=opp.created_at,
            )
            await session.merge(record)
            await session.commit()

    async def get_recent_opportunities(self, limit: int = 50) -> List[OpportunityRecord]:
        async with self.session_factory() as session:
            res = await session.execute(
                select(OpportunityRecord).order_by(OpportunityRecord.created_at.desc()).limit(limit)
            )
            return list(res.scalars().all())

    # --- Attempts ---
    async def record_attempt(self, attempt: LiquidationAttempt) -> None:
        async with self.session_factory() as session:
            record = AttemptRecord(
                attempt_id=attempt.attempt_id,
                opportunity_id=attempt.opportunity_id,
                protocol=attempt.protocol,
                account_id=attempt.account_id,
                chain=attempt.chain.value,
                mode=attempt.mode.value,
                status=attempt.status.value,
                tx_hash=attempt.tx_hash,
                block_number=attempt.block_number,
                detection_block=attempt.detection_block,
                competitor_liquidated=attempt.competitor_liquidated,
                competitor_address=attempt.competitor_address,
                latency_blocks=attempt.latency_blocks,
                repay_token_symbol=attempt.repay_token_symbol,
                repay_amount_raw=str(attempt.repay_amount_raw),
                proceeds_raw=str(attempt.proceeds_raw),
                gas_units_used=attempt.gas_units_used,
                gas_cost_usd=attempt.gas_cost_usd,
                net_pnl_usd=attempt.net_pnl_usd,
                failure_reason=attempt.failure_reason,
                created_at=attempt.created_at,
                finalized_at=attempt.finalized_at,
            )
            await session.merge(record)
            await session.commit()

    async def get_recent_attempts(self, limit: int = 50) -> List[AttemptRecord]:
        async with self.session_factory() as session:
            res = await session.execute(
                select(AttemptRecord).order_by(AttemptRecord.created_at.desc()).limit(limit)
            )
            return list(res.scalars().all())

    # --- Sessions ---
    async def create_session(
        self,
        session_id: str,
        user_email: str,
        user_sub: str,
        role: UserRole,
        duration_seconds: int = 3600 * 12,
    ) -> None:
        now = time.time()
        async with self.session_factory() as session:
            record = UserSessionRecord(
                session_id=session_id,
                user_email=user_email,
                user_sub=user_sub,
                role=role.value,
                created_at=now,
                last_active_at=now,
                last_auth_time=now,
                expires_at=now + duration_seconds,
            )
            session.add(record)
            await session.commit()

    async def get_session(self, session_id: str) -> Optional[UserSessionRecord]:
        now = time.time()
        async with self.session_factory() as session:
            record = await session.get(UserSessionRecord, session_id)
            if not record:
                return None
            if record.expires_at < now:
                await session.delete(record)
                await session.commit()
                return None
            # Update last active
            record.last_active_at = now
            await session.commit()
            return record

    async def delete_session(self, session_id: str) -> None:
        async with self.session_factory() as session:
            record = await session.get(UserSessionRecord, session_id)
            if record:
                await session.delete(record)
                await session.commit()

    # --- Audit Logs ---
    async def log_audit(
        self,
        audit_id: str,
        user_email: str,
        user_sub: str,
        role: UserRole,
        action: str,
        details: Dict[str, Any],
        ip_address: str = "127.0.0.1",
        success: bool = True,
    ) -> None:
        async with self.session_factory() as session:
            record = AuditLogRecord(
                id=audit_id,
                timestamp=time.time(),
                user_email=user_email,
                user_sub=user_sub,
                role=role.value,
                action=action,
                details_json=json.dumps(details),
                ip_address=ip_address,
                success=success,
            )
            session.add(record)
            await session.commit()

    async def get_audit_logs(self, limit: int = 50) -> List[AuditLogRecord]:
        async with self.session_factory() as session:
            res = await session.execute(
                select(AuditLogRecord).order_by(AuditLogRecord.timestamp.desc()).limit(limit)
            )
            return list(res.scalars().all())
