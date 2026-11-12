"""Application configuration settings loaded safely from environment variables."""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from liquidator.core.models import BotMode, UserRole
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core Application Settings
    APP_NAME: str = "Modular Liquidation Bot"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    DATABASE_URL: str = "sqlite+aiosqlite:///./liquidator.db"
    SECRET_KEY: str = Field(
        default="development-insecure-secret-key-must-be-changed-in-production-32b",
        description="Used for signing session cookies and CSRF tokens"
    )

    # Operating Modes (Dry run by default!)
    DEFAULT_BOT_MODE: BotMode = BotMode.DRY_RUN
    ALLOW_LIVE_EXECUTION: bool = False  # Global safety latch: live mode impossible unless True

    # Google OpenID Connect (OIDC) Settings
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/callback"
    AUTH_ALLOWLIST: str = Field(
        default="",
        description="Comma-separated list of approved Google emails or sub IDs"
    )
    ADMIN_ALLOWLIST: str = Field(
        default="",
        description="Comma-separated list of Google emails with Administrator role"
    )
    SESSION_IDLE_TIMEOUT_MINUTES: int = 30
    SESSION_ABSOLUTE_TIMEOUT_HOURS: int = 12
    DEV_AUTH_BYPASS: bool = False  # Allows local offline dev testing with mock identity

    # Web & API Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: List[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]

    # Chain RPC Providers
    BASE_RPC_URL: str = Field(default="https://mainnet.base.org", description="Base EVM RPC")
    SOLANA_RPC_URL: str = Field(default="https://api.mainnet-beta.solana.com", description="Solana RPC")
    SUI_RPC_URL: str = Field(default="https://fullnode.mainnet.sui.io:443", description="Sui RPC")
    APTOS_RPC_URL: str = Field(default="https://fullnode.mainnet.aptoslabs.com/v1", description="Aptos RPC")

    # Contract Addresses (Moonwell on Base)
    MOONWELL_COMPTROLLER_ADDRESS: str = "0xfBb21156Cc56a1745773299298379B5e7834E35A"
    BALANCER_VAULT_ADDRESS: str = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"
    UNISWAP_V3_ROUTER_ADDRESS: str = "0x2626664c2603336E57B271c5C0b26F421741e481"
    UNISWAP_V3_QUOTER_ADDRESS: str = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    EXECUTOR_CONTRACT_ADDRESS: Optional[str] = None

    # Credentials (Must be provided securely in live mode)
    SIGNER_PRIVATE_KEY: Optional[str] = None

    # Risk Parameters
    MAX_REPAY_USD: Decimal = Decimal("25000.00")
    MIN_PROFIT_USD: Decimal = Decimal("15.00")
    MAX_SLIPPAGE_BPS: int = 100  # 1%
    MAX_GAS_GWEI: Decimal = Decimal("5.0")
    MAX_DAILY_LOSS_USD: Decimal = Decimal("1000.00")
    SIMULATION_COOLDOWN_SECONDS: int = 60
    MAX_ORACLE_STALENESS_SECONDS: int = 300

    @property
    def parsed_allowlist(self) -> List[str]:
        if not self.AUTH_ALLOWLIST.strip():
            return []
        return [email.strip().lower() for email in self.AUTH_ALLOWLIST.split(",") if email.strip()]

    @property
    def parsed_admin_allowlist(self) -> List[str]:
        if not self.ADMIN_ALLOWLIST.strip():
            return []
        return [email.strip().lower() for email in self.ADMIN_ALLOWLIST.split(",") if email.strip()]

    def get_role_for_identity(self, email: str, sub: str) -> Optional[UserRole]:
        """Determine role based on allowlists. Returns None if unauthorized."""
        if self.DEV_AUTH_BYPASS:
            return UserRole.ADMINISTRATOR

        email_clean = email.strip().lower()
        sub_clean = sub.strip()

        # Check admin allowlist
        if email_clean in self.parsed_admin_allowlist or sub_clean in self.parsed_admin_allowlist:
            return UserRole.ADMINISTRATOR

        # Check standard allowlist
        if email_clean in self.parsed_allowlist or sub_clean in self.parsed_allowlist:
            return UserRole.OPERATOR

        return None
