"""Token decimal normalization, fixed-point math, and reconciliation invariant gates."""

from __future__ import annotations

import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional, Tuple

from liquidator.core.models import (
    ChainType,
    InvariantCheckResult,
    ReasonCode,
    Token,
)

# Constant precision for USD reporting (6 decimal places: $0.000001)
USD_PRECISION = Decimal("0.000001")
ETH_DECIMALS = 18
SOL_DECIMALS = 9
SUI_DECIMALS = 9
APTOS_DECIMALS = 8


def raw_to_norm(amount_raw: int, decimals: int) -> Decimal:
    """Convert raw integer token units into normalized human-readable Decimal."""
    if decimals < 0 or decimals > 36:
        raise ValueError(f"Invalid token decimals: {decimals}")
    return Decimal(amount_raw) / (Decimal(10) ** decimals)


def norm_to_raw(amount_norm: Decimal, decimals: int) -> int:
    """Convert normalized Decimal into raw integer token units (truncated)."""
    if decimals < 0 or decimals > 36:
        raise ValueError(f"Invalid token decimals: {decimals}")
    scaled = amount_norm * (Decimal(10) ** decimals)
    return int(scaled.to_integral_value(rounding=ROUND_DOWN))


def quantize_usd(amount: Decimal) -> Decimal:
    """Format USD decimal to fixed-point 6 decimal places."""
    return amount.quantize(USD_PRECISION, rounding=ROUND_HALF_UP)


def native_gas_to_debt_units(
    gas_native_raw: int,
    chain: ChainType,
    native_price_usd: Decimal,
    debt_token: Token,
    max_price_staleness_sec: int = 300,
) -> Tuple[int, Decimal]:
    """
    Convert native chain gas units (wei, lamports, MIST, octas) into debt asset base units.

    Returns:
        (gas_in_debt_units_raw, gas_cost_usd_6dec)
    Raises:
        ValueError if prices are non-positive or stale.
    """
    now = time.time()
    if native_price_usd <= Decimal("0"):
        raise ValueError(f"Invalid native token price: {native_price_usd}")
    if debt_token.price_usd <= Decimal("0"):
        raise ValueError(f"Invalid debt token price: {debt_token.price_usd}")

    if (now - debt_token.price_timestamp) > max_price_staleness_sec:
        raise ValueError(
            f"Debt token {debt_token.symbol} price is stale ({now - debt_token.price_timestamp:.1f}s old)"
        )

    # Determine native chain decimals
    if chain == ChainType.EVM:
        native_decimals = ETH_DECIMALS
    elif chain == ChainType.SOLANA:
        native_decimals = SOL_DECIMALS
    elif chain == ChainType.SUI:
        native_decimals = SUI_DECIMALS
    elif chain == ChainType.APTOS:
        native_decimals = APTOS_DECIMALS
    else:
        raise ValueError(f"Unsupported chain type: {chain}")

    # Convert native gas units to normalized native token amount
    gas_native_norm = raw_to_norm(gas_native_raw, native_decimals)

    # Gas cost in USD
    gas_cost_usd = quantize_usd(gas_native_norm * native_price_usd)

    # Convert USD cost to debt asset normalized amount
    # debt_norm = gas_cost_usd / debt_token.price_usd
    gas_in_debt_norm = (gas_native_norm * native_price_usd) / debt_token.price_usd

    # Convert to raw integer units of the debt token
    gas_in_debt_raw = norm_to_raw(gas_in_debt_norm, debt_token.decimals)

    return gas_in_debt_raw, gas_cost_usd


def verify_reconciliation_invariant(
    collateral_sold_proceeds_raw: int,
    debt_to_repay_raw: int,
    flash_premium_raw: int,
    gas_in_debt_raw: int,
    min_profit_in_debt_raw: int,
    debt_token: Token,
) -> InvariantCheckResult:
    """
    Enforces the mandatory reconciliation invariant:
      collateral_sold_proceeds_in_debt_asset >=
          debt_to_repay + flash_premium + gas_and_network_fees_in_debt_asset + min_profit_in_debt_asset

    All calculations done strictly in debt asset base units.
    """
    total_required_costs_raw = (
        debt_to_repay_raw + flash_premium_raw + gas_in_debt_raw + min_profit_in_debt_raw
    )
    net_proceeds_raw = collateral_sold_proceeds_raw - (
        debt_to_repay_raw + flash_premium_raw + gas_in_debt_raw
    )

    # Convert net profit in debt asset to USD
    net_profit_norm = raw_to_norm(net_proceeds_raw, debt_token.decimals)
    net_profit_usd = quantize_usd(net_profit_norm * debt_token.price_usd)

    passed = collateral_sold_proceeds_raw >= total_required_costs_raw

    rejection_reason: Optional[ReasonCode] = None
    detail = ""

    if not passed:
        if collateral_sold_proceeds_raw < debt_to_repay_raw + flash_premium_raw:
            rejection_reason = ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE
            detail = (
                f"Collateral proceeds ({collateral_sold_proceeds_raw}) fail to cover debt "
                f"({debt_to_repay_raw}) and flash fee ({flash_premium_raw})"
            )
        elif collateral_sold_proceeds_raw < debt_to_repay_raw + flash_premium_raw + gas_in_debt_raw:
            rejection_reason = ReasonCode.GAS_EXCEEDS_PROFIT
            detail = (
                f"Gas in debt units ({gas_in_debt_raw}) exceeds gross profit "
                f"({collateral_sold_proceeds_raw - debt_to_repay_raw - flash_premium_raw})"
            )
        else:
            rejection_reason = ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE
            detail = (
                f"Net proceeds ({net_proceeds_raw}) lower than configured minimum profit "
                f"({min_profit_in_debt_raw})"
            )
    else:
        detail = f"Invariant verified: net profit USD = ${net_profit_usd}"

    return InvariantCheckResult(
        passed=passed,
        collateral_proceeds_raw=collateral_sold_proceeds_raw,
        required_repay_raw=debt_to_repay_raw,
        flash_premium_raw=flash_premium_raw,
        gas_cost_in_debt_raw=gas_in_debt_raw,
        min_profit_in_debt_raw=min_profit_in_debt_raw,
        net_proceeds_raw=net_proceeds_raw,
        net_profit_usd=net_profit_usd,
        rejection_reason=rejection_reason,
        detail=detail,
    )
