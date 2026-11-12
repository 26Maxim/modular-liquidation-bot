"""SQLAlchemy ORM models for liquidation bot persistence."""

from __future__ import annotations

import time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class BorrowerPositionRecord(Base):
    """Monitored borrower debt and collateral positions."""
    __tablename__ = "borrower_positions"

    id = Column(String(128), primary_key=True)  # {chain}_{protocol}_{account_id}
    chain = Column(String(32), nullable=False)
    protocol = Column(String(64), nullable=False)
    account_id = Column(String(128), nullable=False)
    debt_asset = Column(String(128), nullable=False)
    collateral_asset = Column(String(128), nullable=False)
    debt_amount_raw = Column(String(78), nullable=False)  # Store large 256-bit integers as string
    collateral_amount_raw = Column(String(78), nullable=False)
    health_factor = Column(Numeric(18, 6), nullable=False)
    block_number = Column(BigInteger, nullable=False)
    last_updated = Column(Float, default=time.time)
    liquidation_price_usd = Column(Numeric(18, 6), nullable=True)
    tier = Column(String(16), default="COLD")
    collateral_symbol = Column(String(32), nullable=True)
    debt_symbol = Column(String(32), nullable=True)
    distance_to_liq_pct = Column(Numeric(10, 4), nullable=True)
    metadata_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_borrower_health", "chain", "protocol", "health_factor"),
        Index("idx_borrower_tier", "tier"),
        Index("idx_borrower_account", "account_id"),
    )


class OpportunityRecord(Base):
    """Detected and evaluated liquidation opportunities."""
    __tablename__ = "opportunities"

    opportunity_id = Column(String(128), primary_key=True)
    chain = Column(String(32), nullable=False)
    protocol = Column(String(64), nullable=False)
    borrower_address = Column(String(128), nullable=False)
    debt_asset_address = Column(String(128), nullable=False)
    collateral_asset_address = Column(String(128), nullable=False)
    repay_amount_raw = Column(String(78), nullable=False)
    expected_collateral_raw = Column(String(78), nullable=False)
    expected_proceeds_raw = Column(String(78), nullable=False)
    funding_source = Column(String(64), nullable=False)
    disposal_venue = Column(String(64), nullable=False)
    estimated_gas_usd = Column(Numeric(18, 6), nullable=False)
    expected_net_profit_usd = Column(Numeric(18, 6), nullable=False)
    invariant_passed = Column(Boolean, nullable=False)
    rejection_reason = Column(String(64), nullable=True)
    detection_block = Column(BigInteger, nullable=True)
    created_at = Column(Float, default=time.time)

    __table_args__ = (
        Index("idx_opp_created", "created_at"),
        Index("idx_opp_invariant", "invariant_passed"),
    )


class SimulationCooldownRecord(Base):
    """
    Simulation cooldown state to prevent RPC exhaustion.
    Cooldown key: (protocol, account_id, debt_asset, collateral_asset)
    """
    __tablename__ = "simulation_cooldowns"

    cooldown_key = Column(String(256), primary_key=True)
    protocol = Column(String(64), nullable=False)
    account_id = Column(String(128), nullable=False)
    debt_asset = Column(String(128), nullable=False)
    collateral_asset = Column(String(128), nullable=False)
    last_failed_at = Column(Float, nullable=False)
    oracle_price_at_failure = Column(Numeric(18, 6), nullable=False)
    reserves_hash_at_failure = Column(String(64), nullable=False)
    reason_code = Column(String(64), nullable=False)


class RejectionLogRecord(Base):
    """
    Deduplicated rejection log entries.
    Deduplication key: (protocol, account_id, reason_code)
    DO NOT include volatile values (gas price, amounts) in the deduplication key!
    """
    __tablename__ = "rejection_logs"

    dedup_key = Column(String(256), primary_key=True)  # {protocol}:{account_id}:{reason_code}
    protocol = Column(String(64), nullable=False)
    account_id = Column(String(128), nullable=False)
    reason_code = Column(String(64), nullable=False)
    occurrence_count = Column(Integer, default=1)
    first_seen_at = Column(Float, default=time.time)
    last_seen_at = Column(Float, default=time.time)
    # Store volatile details in a JSON/Text field, not in dedup key:
    last_details_json = Column(Text, nullable=False)


class AttemptRecord(Base):
    """Liquidation attempts, both simulation-only and broadcasted."""
    __tablename__ = "attempts"

    attempt_id = Column(String(128), primary_key=True)
    opportunity_id = Column(String(128), nullable=False)
    protocol = Column(String(64), nullable=False)
    account_id = Column(String(128), nullable=False)
    chain = Column(String(32), nullable=False)
    mode = Column(String(16), nullable=False)  # DRY_RUN or LIVE
    status = Column(String(32), nullable=False)
    tx_hash = Column(String(128), nullable=True)
    block_number = Column(BigInteger, nullable=True)
    detection_block = Column(BigInteger, nullable=True)
    competitor_liquidated = Column(Boolean, default=False)
    competitor_address = Column(String(128), nullable=True)
    latency_blocks = Column(Integer, nullable=True)
    repay_token_symbol = Column(String(16), nullable=False)
    repay_amount_raw = Column(String(78), nullable=False)
    proceeds_raw = Column(String(78), default="0")
    gas_units_used = Column(BigInteger, default=0)
    gas_cost_usd = Column(Numeric(18, 6), default=0)
    net_pnl_usd = Column(Numeric(18, 6), default=0)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(Float, default=time.time)
    finalized_at = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_attempt_status", "status"),
        Index("idx_attempt_created", "created_at"),
    )


class UserSessionRecord(Base):
    """Server-side opaque sessions."""
    __tablename__ = "user_sessions"

    session_id = Column(String(128), primary_key=True)
    user_email = Column(String(128), nullable=False)
    user_sub = Column(String(128), nullable=False)
    role = Column(String(32), nullable=False)
    created_at = Column(Float, default=time.time)
    last_active_at = Column(Float, default=time.time)
    last_auth_time = Column(Float, default=time.time)  # Used for sensitive action re-auth check
    expires_at = Column(Float, nullable=False)


class AuditLogRecord(Base):
    """Immutable audit log of operator actions."""
    __tablename__ = "audit_logs"

    id = Column(String(128), primary_key=True)
    timestamp = Column(Float, default=time.time)
    user_email = Column(String(128), nullable=False)
    user_sub = Column(String(128), nullable=False)
    role = Column(String(32), nullable=False)
    action = Column(String(64), nullable=False)
    details_json = Column(Text, nullable=False)
    ip_address = Column(String(64), default="127.0.0.1")
    success = Column(Boolean, default=True)

    __table_args__ = (
        Index("idx_audit_timestamp", "timestamp"),
        Index("idx_audit_action", "action"),
    )
