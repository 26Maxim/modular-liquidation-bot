"""Unit tests for decimal normalization, gas conversions, and reconciliation invariant gates."""

import time
from decimal import Decimal

from liquidator.core.models import ChainType, ReasonCode, Token
from liquidator.core.scaling import (
    native_gas_to_debt_units,
    norm_to_raw,
    raw_to_norm,
    verify_reconciliation_invariant,
)


def test_token_decimal_scaling_non_18_decimals():
    """Verify non-18 decimal tokens (USDC 6 dec, WBTC 8 dec) scale accurately without precision loss."""
    # USDC (6 decimals): 1,234.56 USDC = 1,234,560,000 raw
    raw_usdc = 1_234_560_000
    norm_usdc = raw_to_norm(raw_usdc, decimals=6)
    assert norm_usdc == Decimal("1234.56")
    assert norm_to_raw(norm_usdc, decimals=6) == raw_usdc

    # WBTC (8 decimals): 0.12345678 BTC = 12,345,678 raw
    raw_wbtc = 12_345_678
    norm_wbtc = raw_to_norm(raw_wbtc, decimals=8)
    assert norm_wbtc == Decimal("0.12345678")
    assert norm_to_raw(norm_wbtc, decimals=8) == raw_wbtc

    # Non-EVM: Solana SOL (9 decimals): 1.5 SOL = 1,500,000,000 lamports
    raw_sol = 1_500_000_000
    norm_sol = raw_to_norm(raw_sol, decimals=9)
    assert norm_sol == Decimal("1.5")
    assert norm_to_raw(norm_sol, decimals=9) == raw_sol


def test_native_gas_to_debt_units_conversion():
    """Verify conversion of native chain units (wei, lamports, MIST) into debt asset units & 6-dec USD."""
    # Scenario: Base EVM with ETH = $3,000, Debt asset = USDC (6 dec, $1.00)
    # Gas cost = 300,000 units * 0.1 gwei = 30,000 gwei = 30,000 * 10^9 wei = 30,000,000,000,000 wei (0.00003 ETH)
    # 0.00003 ETH * $3000 = $0.090000 USD
    # In USDC (6 decimals): $0.09 = 90,000 raw units
    usdc = Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )
    gas_native_wei = 30_000_000_000_000
    eth_price_usd = Decimal("3000.00")

    gas_debt_raw, gas_usd = native_gas_to_debt_units(
        gas_native_raw=gas_native_wei,
        chain=ChainType.EVM,
        native_price_usd=eth_price_usd,
        debt_token=usdc,
    )

    assert gas_usd == Decimal("0.090000")
    assert gas_debt_raw == 90_000


def test_reconciliation_invariant_gate_success():
    """Verify invariant gate passes when proceeds exceed repayment, fees, and minimum profit."""
    usdc = Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )

    # Debt: $10,000 USDC (10,000,000,000 raw)
    # Flash fee: $0 (Balancer 0%)
    # Gas in debt units: $1.50 USDC (1,500,000 raw)
    # Min profit: $20 USDC (20,000,000 raw)
    # Total required = 10,021,500,000 raw
    # Collateral sold proceeds = $10,500 USDC (10,500,000,000 raw)
    result = verify_reconciliation_invariant(
        collateral_sold_proceeds_raw=10_500_000_000,
        debt_to_repay_raw=10_000_000_000,
        flash_premium_raw=0,
        gas_in_debt_raw=1_500_000,
        min_profit_in_debt_raw=20_000_000,
        debt_token=usdc,
    )

    assert result.passed is True
    assert result.rejection_reason is None
    # Net proceeds = 10,500,000,000 - (10,000,000,000 + 1,500,000) = 498,500,000 ($498.50)
    assert result.net_profit_usd == Decimal("498.500000")


def test_reconciliation_invariant_gate_failure_modes():
    """Verify invariant gate rejects when proceeds are insufficient."""
    usdc = Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )

    # Case 1: Proceeds lower than debt to repay
    res_underflow = verify_reconciliation_invariant(
        collateral_sold_proceeds_raw=9_900_000_000,
        debt_to_repay_raw=10_000_000_000,
        flash_premium_raw=0,
        gas_in_debt_raw=1_000_000,
        min_profit_in_debt_raw=15_000_000,
        debt_token=usdc,
    )
    assert res_underflow.passed is False
    assert res_underflow.rejection_reason == ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE

    # Case 2: Proceeds cover debt, but gas wipes out profit
    res_gas_loss = verify_reconciliation_invariant(
        collateral_sold_proceeds_raw=10_005_000_000,  # $5 gross profit
        debt_to_repay_raw=10_000_000_000,
        flash_premium_raw=0,
        gas_in_debt_raw=10_000_000,  # $10 gas!
        min_profit_in_debt_raw=15_000_000,
        debt_token=usdc,
    )
    assert res_gas_loss.passed is False
    assert res_gas_loss.rejection_reason == ReasonCode.GAS_EXCEEDS_PROFIT

    # Case 3: Proceeds cover gas, but fail minimum profit floor
    res_min_profit = verify_reconciliation_invariant(
        collateral_sold_proceeds_raw=10_010_000_000,  # $10 gross profit
        debt_to_repay_raw=10_000_000_000,
        flash_premium_raw=0,
        gas_in_debt_raw=1_000_000,  # $1 gas -> $9 net
        min_profit_in_debt_raw=20_000_000,  # Requires $20
        debt_token=usdc,
    )
    assert res_min_profit.passed is False
    assert res_min_profit.rejection_reason == ReasonCode.UNPROFITABLE_AFTER_SLIPPAGE
