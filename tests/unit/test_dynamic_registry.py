"""Unit and API integration tests for dynamic multi-chain registry and self-funded mode."""

import pytest
from httpx import ASGITransport, AsyncClient
from liquidator.app import create_app
from liquidator.core.config import AppSettings
from liquidator.core.models import ChainType, Token
from liquidator.funding.self_funded import SelfFundedAdapter
from liquidator.protocols.generic_lending_adapter import GenericLendingAdapter
from decimal import Decimal


def test_self_funded_adapter():
    adapter = SelfFundedAdapter(wallet_address="0x1234567890123456789012345678901234567890", max_capital_usd=5000.0)
    assert adapter.name == "self_funded_wallet"
    assert adapter.chain == ChainType.EVM

    token = Token(
        chain=ChainType.EVM,
        address="0xUSDC",
        symbol="USDC",
        decimals=6,
        price_usd=Decimal("1.00"),
    )
    # Fee is 0 bps
    fee = adapter.calculate_fee(token, 1_000_000)
    assert fee == 0

    # Build direct execution payload
    call = adapter.build_flash_loan_call(token, 1_000_000, "0xReceiver", b"")
    assert call["funding_mode"] == "SELF_FUNDED"
    assert call["method"] == "directExecute"


def test_generic_lending_adapter_calculations():
    adapter = GenericLendingAdapter(
        protocol_id="sonic_rings",
        chain_id="sonic",
        contract_address="0x5C808E56B486c478a89F2A6F14Fd5c2196627C44",
        rpc_url="https://rpc.soniclabs.com",
    )
    assert adapter.protocol_id == "sonic_rings"
    assert adapter.chain == ChainType.EVM

    from liquidator.core.models import BorrowerPosition
    pos = BorrowerPosition(
        chain=ChainType.EVM,
        protocol="sonic_rings",
        account_id="0xBorrower",
        debt_asset="0xDebt",
        collateral_asset="0xCollateral",
        debt_amount_raw=1000 * 10**6,
        collateral_amount_raw=2000 * 10**18,
        health_factor=Decimal("0.95"),
        block_number=100,
    )
    debt_token = Token(chain=ChainType.EVM, address="0xDebt", symbol="USDC", decimals=6, price_usd=Decimal("1.00"))
    col_token = Token(chain=ChainType.EVM, address="0xCollateral", symbol="WETH", decimals=18, price_usd=Decimal("2000.00"))

    max_repay, seize_amt = adapter.calculate_allowable_repay_and_collateral(pos, debt_token, col_token)
    # Close factor 0.50 -> max_repay = 500 USDC
    assert max_repay == 500 * 10**6
    # Incentive 1.10 -> 550 USD seize value / 2000 USD/WETH = 0.275 WETH
    assert seize_amt == int(Decimal("0.275") * Decimal(10**18))


@pytest.mark.asyncio
async def test_chain_registry_api():
    settings = AppSettings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DEV_AUTH_BYPASS=True,
    )
    app = create_app(settings)
    await app.state.db.init_models()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Register a new chain
        new_chain = {
            "chain_id": "test_chain",
            "chain_name": "Test Chain",
            "chain_type": "EVM",
            "rpc_url": "https://rpc.testchain.org",
            "protocol_type": "compound_v2",
            "protocol_name": "Test Protocol",
            "comptroller_or_pool_address": "0x1111111111111111111111111111111111111111",
            "funding_mode": "SELF_FUNDED",
            "is_active": True,
        }
        res_post = await client.post("/api/v1/chains", json=new_chain)
        assert res_post.status_code == 200
        assert res_post.json()["status"] == "ok"

        # List registered chains
        res_get = await client.get("/api/v1/chains")
        assert res_get.status_code == 200
        chains = res_get.json()
        ids = [c["chain_id"] for c in chains]
        assert "test_chain" in ids

        # Toggle active
        res_toggle = await client.post("/api/v1/chains/test_chain/toggle", json={"is_active": False})
        assert res_toggle.status_code == 200
        assert res_toggle.json()["is_active"] is False

        # Delete chain
        res_del = await client.delete("/api/v1/chains/test_chain")
        assert res_del.status_code == 200

        # Purge red ocean endpoint
        res_cleanup = await client.post("/api/v1/cleanup")
        assert res_cleanup.status_code == 200
        assert res_cleanup.json()["status"] == "ok"
