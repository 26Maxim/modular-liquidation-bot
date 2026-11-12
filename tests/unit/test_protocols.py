"""Unit tests for lending protocol calculations across EVM and non-EVM adapters."""

import time
from decimal import Decimal

from liquidator.core.models import (
    BorrowerPosition,
    ChainType,
    Token,
)
from liquidator.protocols.moonwell_adapter import MoonwellAdapter
from liquidator.protocols.seamless_adapter import SeamlessAdapter


def test_moonwell_compound_v2_math():
    """Verify Moonwell close factor (50%) and liquidation incentive (10% bonus)."""
    adapter = MoonwellAdapter()

    usdc = Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )
    weth = Token(
        chain=ChainType.EVM,
        address="0x4200000000000000000000000000000000000006",
        symbol="WETH",
        decimals=18,
        price_usd=Decimal("3000.00"),
        price_timestamp=time.time(),
    )

    # Borrower has 10,000 USDC debt and 5 WETH ($15,000) collateral
    pos = BorrowerPosition(
        chain=ChainType.EVM,
        protocol="moonwell_base",
        account_id="0xBorrower1",
        debt_asset=usdc.address,
        collateral_asset=weth.address,
        debt_amount_raw=10_000 * 10**6,  # 10,000 USDC
        collateral_amount_raw=5 * 10**18,   # 5 WETH
        health_factor=Decimal("0.92"),
        block_number=20_000_000,
    )

    repay_raw, seize_raw = adapter.calculate_allowable_repay_and_collateral(pos, usdc, weth)

    # 50% close factor = 5,000 USDC repay
    assert repay_raw == 5_000 * 10**6

    # Seize value = $5,000 * 1.10 = $5,500
    # In WETH at $3,000/ETH = 5,500 / 3,000 = 1.833333333333333333 WETH
    expected_weth_norm = Decimal("5500") / Decimal("3000")
    expected_weth_raw = int(expected_weth_norm * Decimal(10**18))
    assert seize_raw == expected_weth_raw


def test_moonwell_capped_when_collateral_insufficient():
    """Verify allowable repay is scaled down if borrower collateral is lower than expected seize."""
    adapter = MoonwellAdapter()

    usdc = Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )
    weth = Token(
        chain=ChainType.EVM,
        address="0x4200000000000000000000000000000000000006",
        symbol="WETH",
        decimals=18,
        price_usd=Decimal("3000.00"),
        price_timestamp=time.time(),
    )

    # Borrower has 10,000 USDC debt but only 0.5 WETH ($1,500) collateral
    pos = BorrowerPosition(
        chain=ChainType.EVM,
        protocol="moonwell_base",
        account_id="0xBorrowerUndercollateralized",
        debt_asset=usdc.address,
        collateral_asset=weth.address,
        debt_amount_raw=10_000 * 10**6,
        collateral_amount_raw=int(Decimal("0.5") * Decimal(10**18)),  # 0.5 WETH
        health_factor=Decimal("0.50"),
        block_number=20_000_000,
    )

    repay_raw, seize_raw = adapter.calculate_allowable_repay_and_collateral(pos, usdc, weth)

    # Should seize all 0.5 WETH
    assert seize_raw == int(Decimal("0.5") * Decimal(10**18))
    # 0.5 WETH * $3000 = $1,500 value.
    # Allowed repay = $1,500 / 1.10 = $1,363.636363 USDC
    assert repay_raw == 1_363_636_363


def test_seamless_aave_v3_math():
    """Verify Seamless adapter health factor threshold close factor scaling."""
    adapter = SeamlessAdapter()

    usdc = Token(
        chain=ChainType.EVM,
        address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
        price_timestamp=time.time(),
    )
    weth = Token(
        chain=ChainType.EVM,
        address="0x4200000000000000000000000000000000000006",
        symbol="WETH",
        decimals=18,
        price_usd=Decimal("3000.00"),
        price_timestamp=time.time(),
    )

    # Health factor 0.90 <= 0.95 -> 100% close factor
    pos = BorrowerPosition(
        chain=ChainType.EVM,
        protocol="seamless_base",
        account_id="0xBorrowerSevere",
        debt_asset=usdc.address,
        collateral_asset=weth.address,
        debt_amount_raw=2_000 * 10**6,
        collateral_amount_raw=2 * 10**18,
        health_factor=Decimal("0.90"),
        block_number=20_000_000,
    )

    repay_raw, seize_raw = adapter.calculate_allowable_repay_and_collateral(pos, usdc, weth)
    assert repay_raw == 2_000 * 10**6  # 100% close factor applied!
