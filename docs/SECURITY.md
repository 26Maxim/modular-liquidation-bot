# Security Architecture and Threat Model

**Document Classification:** SECURITY POLICY & THREAT MODEL  
**Auditing Status:** NOT AUDITED BY AN INDEPENDENT SECURITY RESEARCHER  
**Notice:** DO NOT DEPLOY REAL ASSETS TO LIVE CONTRACTS WITHOUT FORMAL AUDIT  

---

## 1. Threat Model

The bot operates in adversarial multi-chain MEV environments. Below is the systematic threat matrix and implemented countermeasures:

| Threat | Attack Vector | Implemented Countermeasure | Fail-Closed Policy |
| :--- | :--- | :--- | :--- |
| **Callback Spoofing** | Attacker calls executor contract `receiveFlashLoan` directly with malicious calldata | Contract verifies `msg.sender == address(trustedVault)` and requires `_inFlashLoan == true` | Reverts with `CallerNotVault()` / `NotInFlashLoan()` |
| **Masked Liquidation Loss** | Contract holds pre-existing profit balances; a losing trade draws down pre-existing funds | Contract snapshots `initialDebtBalance` before flash loan and enforces `endingBalance >= initialBalance + minProfit` | Reverts with `InsufficientProfit()` |
| **Dangling Allowances** | Exploited DEX router or lending market drains approved tokens after execution | Approvals to cTokens and SwapRouter are immediately reset to 0 in execution teardown | Token allowances always 0 outside execution block |
| **Reentrancy** | Malicious collateral token triggers reentrant call to `executeFlashLiquidation` | Boolean `_inFlashLoan` reentrancy latch prevents nested execution | Reverts with `Reentrancy()` |
| **Price Impact Slippage** | Flash loan seizes collateral but illiquid DEX pool returns less than debt repayment | Quoter queried for exact sized trade proceeds; invariant gate checks proceeds $> \text{repay} + \text{gas} + \text{min\_profit}$ | Transaction simulation reverts; attempt rejected |
| **Oracle Staleness / Arbitrage** | Bot broadcasts liquidation before on-chain oracle transaction confirms | Oracle watcher tracks feed updates; stale feeds ($> 300$s) trigger fail-closed rejection | Rejects with `ORACLE_STALE` |
| **RPC Desynchronization** | Flaky RPC node returns outdated account debt balance | On-chain static simulation checks exact health factor before broadcast | Simulation fails closed; cooldown triggered |
| **Unauthorized Live Activation** | Compromised web user tries to toggle bot to live mode | Requires Google OIDC login, Admin role, typed confirmation `CONFIRM_LIVE`, recent auth ($\le 15$m), and `ALLOW_LIVE_EXECUTION=true` | Rejects with 403 Forbidden |

---

## 2. On-Chain Contract Guarantees (`FlashLiquidator.sol`)

The Solidity contract deployed for EVM liquidations enforces four immutable invariant checks:

```solidity
// 1. Balance Snapshotting
uint256 initialDebtBalance = IERC20(params.debtToken).balanceOf(address(this));

// 2. Flash loan request
trustedVault.flashLoan(address(this), tokens, amounts, userData);

// 3. Post-execution balance assertion
uint256 finalDebtBalance = IERC20(params.debtToken).balanceOf(address(this));
if (finalDebtBalance < initialDebtBalance + params.minProfitDebtUnits) {
    revert InsufficientProfit(...);
}
```

---

## 3. Residual Risks & Edge Cases

1. **Competitor Front-Running & Calldata Copying:**
   - On public mempools, searchers copy transaction payloads and bribe block builders.
   - *Mitigation:* Live transactions must use private RPC endpoints (e.g., Flashbots Protect, Base Builder RPC).
2. **Failed Transaction Gas Costs:**
   - If another searcher liquidates the borrower in the same block before the bot's transaction, the bot's transaction reverts on-chain and burns gas.
   - *Mitigation:* Atomic simulation checks state immediately prior to submission.
3. **Sequencer Reorganization / Finality:**
   - L2 block reorganizations or unfinalized state can roll back successful liquidations.
   - *Mitigation:* Accounting ledger tracks `PENDING` attempts until receipt reaches verified block depth.

---

## 4. Requirements for Production Readiness

Before broadcasting live transactions with real capital:
1. Conduct an independent third-party smart contract audit of all on-chain executor contracts.
2. Formally verify mathematical invariant proofs and decimal conversion scaling functions.
3. Integrate Hardware Security Module (HSM) or cloud KMS (AWS KMS / GCP Cloud KMS) for transaction signing rather than environment variable private keys.
4. Establish 24/7 pager duty monitoring and emergency runbooks.
