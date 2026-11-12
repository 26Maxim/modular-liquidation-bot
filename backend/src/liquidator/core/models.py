"""Domain models and data structures for the Modular Liquidation Bot."""

from __future__ import annotations

import time
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChainType(str, Enum):
    EVM = "EVM"
    SOLANA = "SOLANA"
    SUI = "SUI"
    APTOS = "APTOS"


class AdapterStatus(str, Enum):
    RESEARCHED = "RESEARCHED"
    READ_ONLY = "READ_ONLY"
    SIMULATION_ONLY = "SIMULATION_ONLY"
    EXECUTION_IMPLEMENTED = "EXECUTION_IMPLEMENTED"
    PRODUCTION_REVIEW_REQUIRED = "PRODUCTION_REVIEW_REQUIRED"


class BotMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


class BotState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    KILLED = "KILLED"


class UserRole(str, Enum):
    VIEWER = "Viewer"
    OPERATOR = "Operator"
    ADMINISTRATOR = "Administrator"


class AttemptOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    REVERTED = "REVERTED"
    SIMULATION_FAILED = "SIMULATION_FAILED"
    REJECTED_BY_POLICY = "REJECTED_BY_POLICY"
    EXPIRED = "EXPIRED"
    DROPPED = "DROPPED"
    PENDING = "PENDING"


class ReasonCode(str, Enum):
    UNPROFITABLE_AFTER_SLIPPAGE = "UNPROFITABLE_AFTER_SLIPPAGE"
    HEALTH_FACTOR_HEALTHY = "HEALTH_FACTOR_HEALTHY"
    INSUFFICIENT_FLASH_LIQUIDITY = "INSUFFICIENT_FLASH_LIQUIDITY"
    GAS_EXCEEDS_PROFIT = "GAS_EXCEEDS_PROFIT"
    ORACLE_STALE = "ORACLE_STALE"
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
    POSITION_COOLDOWN = "POSITION_COOLDOWN"
    ALLOWLIST_REJECTED = "ALLOWLIST_REJECTED"
    REENTRANCY_DETECTED = "REENTRANCY_DETECTED"
    INSUFFICIENT_COLLATERAL = "INSUFFICIENT_COLLATERAL"
    SIMULATION_REVERTED = "SIMULATION_REVERTED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    MAX_NOTIONAL_EXCEEDED = "MAX_NOTIONAL_EXCEEDED"
    UNKNOWN = "UNKNOWN"


class Token(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain: ChainType
    address: str
    symbol: str
    decimals: int
    price_usd: Decimal = Field(default=Decimal("0.0"))
    price_timestamp: float = Field(default_factory=time.time)
    price_source: str = "oracle"


class BorrowerPosition(BaseModel):
    chain: ChainType
    protocol: str
    account_id: str
    debt_asset: str  # Token address / coin type
    collateral_asset: str
    debt_amount_raw: int
    collateral_amount_raw: int
    health_factor: Decimal
    block_number: int
    last_updated: float = Field(default_factory=time.time)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class InvariantCheckResult(BaseModel):
    passed: bool
    collateral_proceeds_raw: int
    required_repay_raw: int
    flash_premium_raw: int
    gas_cost_in_debt_raw: int
    min_profit_in_debt_raw: int
    net_proceeds_raw: int
    net_profit_usd: Decimal
    rejection_reason: Optional[ReasonCode] = None
    detail: str = ""


class LiquidationOpportunity(BaseModel):
    opportunity_id: str
    chain: ChainType
    protocol: str
    borrower_address: str
    debt_asset: Token
    collateral_asset: Token
    repay_amount_raw: int
    expected_collateral_raw: int
    expected_proceeds_raw: int  # proceeds from selling collateral in debt asset units
    funding_source: str
    funding_premium_raw: int
    disposal_venue: str
    estimated_gas_units: int
    estimated_gas_debt_raw: int
    estimated_gas_usd: Decimal
    expected_net_profit_debt_raw: int
    expected_net_profit_usd: Decimal
    invariant_passed: bool
    rejection_reason: Optional[ReasonCode] = None
    detection_block: Optional[int] = None
    created_at: float = Field(default_factory=time.time)


class SimulationResult(BaseModel):
    success: bool
    gas_used: int = 0
    return_data: str = ""
    error_message: Optional[str] = None
    rejection_code: Optional[ReasonCode] = None
    logs: List[str] = Field(default_factory=list)
    simulated_at: float = Field(default_factory=time.time)


class LiquidationAttempt(BaseModel):
    attempt_id: str
    opportunity_id: str
    protocol: str
    account_id: str
    chain: ChainType
    mode: BotMode
    status: AttemptOutcome
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    detection_block: Optional[int] = None
    competitor_liquidated: bool = False
    competitor_address: Optional[str] = None
    latency_blocks: Optional[int] = None
    repay_token_symbol: str
    repay_amount_raw: int
    proceeds_raw: int = 0
    gas_units_used: int = 0
    gas_cost_usd: Decimal = Decimal("0.0")
    net_pnl_usd: Decimal = Decimal("0.0")
    failure_reason: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    finalized_at: Optional[float] = None


class AuditLogEntry(BaseModel):
    id: str
    timestamp: float = Field(default_factory=time.time)
    user_email: str
    user_sub: str
    role: UserRole
    action: str
    details: Dict[str, Any] = Field(default_factory=dict)
    ip_address: str = "127.0.0.1"
    success: bool = True


class RiskParameters(BaseModel):
    max_repay_per_liquidation_usd: Decimal = Field(default=Decimal("25000.00"))
    min_expected_profit_usd: Decimal = Field(default=Decimal("15.00"))
    max_slippage_bps: int = Field(default=100)  # 1.0%
    max_gas_price_gwei: Decimal = Field(default=Decimal("5.0"))
    max_daily_loss_usd: Decimal = Field(default=Decimal("1000.00"))
    max_consecutive_failures: int = Field(default=3)
    cooldown_seconds: int = Field(default=60)
    allowlisted_protocols: List[str] = Field(default_factory=lambda: ["moonwell_base", "seamless_base"])
    allowlisted_tokens: List[str] = Field(default_factory=list)
    allowlisted_venues: List[str] = Field(default_factory=lambda: ["uniswap_v3", "aerodrome", "cetus", "orca"])
    max_oracle_staleness_seconds: int = Field(default=300)


class OraclePriceUpdate(BaseModel):
    chain: ChainType
    asset_address: str
    symbol: str
    price_usd: Decimal
    timestamp: float = Field(default_factory=time.time)
    source: str
    round_id: Optional[int] = None
