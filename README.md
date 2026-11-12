# Modular Multi-Chain Loan Liquidation Bot

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Foundry](https://img.shields.io/badge/foundry-forge-orange.svg)](https://getfoundry.sh/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)
[![Status: Non-Audited](https://img.shields.io/badge/Audit-NOT_AUDITED-red.svg)]()

A modular, multi-chain loan liquidation bot and authenticated mission-control web plane. Prioritizes legitimate liquidation opportunities across EVM (Base L2), Solana, Sui, and Aptos lending protocols while enforcing strict mathematical invariance gates, conservative profit floors, balance snapshotting, and fail-closed safety defaults.

---

## 1. Scope: What the System Does and Does Not Do

### What it Does
- **Modular Multi-Chain Architecture:** Supports pluggable chain adapters (`EVM`, `Solana`, `Sui`, `Aptos`) and lending protocol adapters (`Moonwell`, `Seamless`, `Kamino`, `Scallop`, `Aries`).
- **Mathematical Invariant Gates:** Validates on-chain decimals explicitly across tokens (USDC 6 dec, WBTC 8 dec, SOL 9 dec, SUI 9 dec) and enforces:
  $$\text{Collateral Proceeds}_{\text{debt asset}} \ge \text{Repay} + \text{Flash Fee} + \text{Gas in Debt Units} + \text{Min Profit}$$
- **RPC Protection & Cooldown:** Enforces a $\ge 60$s simulation cooldown on failed checks with an automatic state-change invalidation guard (re-evaluates immediately if oracle prices or pool reserves change).
- **Deduplicated Rejection Logging:** Rejections deduplicate on `(protocol, account_id, reason_code)` while excluding volatile gas prices from the dedup key.
- **Event-Driven Oracle Watcher:** Subscribes to oracle updates and triggers prioritized re-evaluation of monitored positions holding the updated asset.
- **On-Chain Contract Guards (EVM):** `FlashLiquidator.sol` provides pre-execution balance snapshotting, exact allowance teardown, strict callback authentication, and atomic profit assertions.
- **Authenticated Web Control Plane:** Real-time web dashboard featuring Google OpenID Connect (OIDC) with PKCE, opaque server sessions, CSRF protection, RBAC (`Viewer`, `Operator`, `Administrator`), and typed confirmation for sensitive actions (`CONFIRM_LIVE`).

### What it Does NOT Do
- **No Profit Guarantees:** Flash loans eliminate upfront principal requirements, but do NOT eliminate gas execution costs or market slippage. Liquidations can be unprofitable.
- **No Market Manipulation:** Does not manipulate oracles, front-run user borrows, sandwich trades, or exploit protocol bugs. Uses documented permissionless liquidation interfaces only.
- **No Live Trading by Default:** The bot defaults to `DRY_RUN` mode upon every startup. Live transaction broadcasting requires two separate operational gates: `ALLOW_LIVE_EXECUTION=true` in server environment configuration and typed confirmation by an authenticated Administrator.
- **Not an Audited Product:** This software has not undergone independent third-party security review. It is designed for simulation, research, and testbed execution only.

---

## 2. Multi-Chain Maturity Matrix

Each chain and protocol adapter reports its capability maturity status explicitly:

| Chain / Protocol | Ecosystem | Capability Status | Verified Capabilities |
| :--- | :--- | :--- | :--- |
| **Base (EVM)** | EVM L2 | `EXECUTION_IMPLEMENTED` | RPC reads, static call simulation, signed transaction broadcast |
| **Moonwell** | Base EVM | `EXECUTION_IMPLEMENTED` | Compound v2 close factor (50%), 10% bonus, Balancer flash loan, Uniswap V3 swap, Solidity executor |
| **Seamless** | Base EVM | `READ_ONLY` | Aave v3 pool liquidation parameters, health factor close factor calculation |
| **Solana** | SVM | `READ_ONLY` | Slot polling, account balance retrieval, compute budget estimation |
| **Kamino Lend** | Solana SVM | `READ_ONLY` | Obligation parsing, reserve collateral calculation, instruction encoding |
| **Sui** | Move | `READ_ONLY` | Checkpoint polling, SUI MIST balance query, PTB simulation via `devInspect` |
| **Scallop** | Sui Move | `READ_ONLY` | Shared object layout, PTB command structure (`borrow_flash_loan`, `liquidate`, `swap`) |
| **Aptos** | Move | `RESEARCHED` | REST API version polling, Aries Markets entry function specification |

---

## 3. Quickstart: Running in `DRY_RUN` Mode

### Option A: Local Python Environment
1. Ensure Python 3.12+ and Foundry (`forge`) are installed.
2. Clone repository and copy template configuration:
   ```bash
   cp .env.example .env
   ```
3. Install dependencies:
   ```bash
   pip install -e ".[dev]"
   ```
4. Run the web control plane:
   ```bash
   uvicorn liquidator.app:create_app --factory --host 0.0.0.0 --port 8000
   ```
5. Open `http://localhost:8000` in your browser.

### Option B: Docker / Docker Compose
Run the entire stack in an isolated container:
```bash
docker compose up --build
```
Access the dashboard at `http://localhost:8000`.

---

## 4. Google OpenID Connect (OIDC) Setup

To configure production-grade authentication:
1. Go to the [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
2. Create an **OAuth 2.0 Client ID** (Application type: *Web application*).
3. Add Authorized Redirect URI:
   ```
   http://localhost:8000/auth/callback
   ```
4. Set the credentials in your `.env` file:
   ```bash
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your-client-secret
   GOOGLE_REDIRECT_URI=http://localhost:8000/auth/callback
   AUTH_ALLOWLIST=operator@example.com,admin@example.com
   ADMIN_ALLOWLIST=admin@example.com
   DEV_AUTH_BYPASS=false
   ```
> [!NOTE]
> For offline local development and testing, set `DEV_AUTH_BYPASS=true` to enable the one-click development mock login. The system strictly disables `DEV_AUTH_BYPASS` whenever `ENVIRONMENT=production`.

---

## 5. Verification & Testing

Run all unit, integration, and on-chain adversarial contract tests:
```bash
make verify
```

Or run individual test suites:
- **Smart Contract Tests:** `make test-contracts` (runs Foundry suite in `contracts/evm/test`)
- **Unit Tests:** `make test-unit` (tests scaling, invariants, cooldown, protocol math, auth)
- **Integration Tests:** `make test-integration` (tests discovery, oracle watcher, opportunity evaluation)
- **End-to-End Pipeline:** `make test-e2e` (tests complete dry-run lifecycle and web API)
- **Linter & Typecheck:** `make lint` and `make typecheck`

---

## 6. Live Mode Prerequisites and Strict Warnings

> [!CAUTION]
> Live mode transmits signed transactions to the live blockchain. Real funds and native gas tokens are consumed.

To enable live execution:
1. `ALLOW_LIVE_EXECUTION=true` must be configured in server environment variables.
2. An Administrator must sign in through Google OIDC.
3. The Administrator must navigate to *Controls & Mode* and type `CONFIRM_LIVE`.
4. Authentication must have occurred within the last 15 minutes (recent auth check).
5. A funded private key (`SIGNER_PRIVATE_KEY`) must be supplied via secure secret management.
6. The on-chain executor contract address must be set (`EXECUTOR_CONTRACT_ADDRESS`).

---

## 7. Known Limitations
- **Solana, Sui, and Aptos live execution:** Currently limited to read-only discovery and transaction simulation. Live execution is only implemented on Base EVM.
- **Private Relay RPC:** When running on Base mainnet, use a private builder endpoint (e.g. Flashbots Protect or Base Builder RPC) to prevent public mempool calldata copying.
- **RPC Availability:** If an RPC node returns inconsistent or lagging state, static simulation will fail closed and trigger a simulation cooldown.
