# Operational Runbook & Incident Response Guide

**Version:** 1.0.0  
**Target Environment:** Linux / Docker  

---

## 1. Operating Modes & Bot Lifecycle

The bot supports two operational modes:
- `DRY_RUN`: Complete position discovery, invariant checking, DEX quoting, and static simulation without signing or broadcasting transactions. This is the **hardcoded default upon every restart**.
- `LIVE`: Signs and broadcasts transactions only if `ALLOW_LIVE_EXECUTION=true` is set in server environment variables and an Operator/Admin submits typed confirmation `CONFIRM_LIVE`.

### Bot Lifecycle States
1. `RUNNING`: Actively monitoring positions and evaluating opportunities.
2. `PAUSED`: Evaluating positions, but simulation and execution are suspended (triggered automatically by circuit breakers).
3. `STOPPED`: Monitoring loop stopped.
4. `KILLED`: Immediate emergency kill switch engaged. All new executions rejected unconditionally.

---

## 2. Emergency Response: Kill Switch Procedures

### 2.1 Triggering the Kill Switch
- **Via Web Dashboard:** Click the red `🛑 KILL SWITCH` button on the navigation bar, enter an incident reason, and confirm.
- **Via REST API:**
  ```bash
  curl -X POST http://localhost:8000/api/v1/controls/kill \
    -H "Content-Type: application/json" \
    -d '{"reason": "Oracle deviation incident detected"}'
  ```

### 2.2 Kill Switch Limitations
> [!IMPORTANT]
> The software kill switch stops future evaluations and locally queued attempts immediately.
> However, **it cannot cancel transactions that have already been broadcast to the blockchain network**.
> Once a transaction enters the public mempool or block builder queue, it will execute or revert on-chain.

---

## 3. Circuit Breakers & Auto-Trip Conditions

The policy engine automatically trips to `PAUSED` when:
1. **Consecutive Failures:** Reaches $\ge 3$ consecutive execution/simulation failures.
2. **Daily Loss Ceiling:** Accumulated transaction gas losses exceed `MAX_DAILY_LOSS_USD` ($1,000.00 default).
3. **Oracle Staleness:** Oracle watcher lag exceeds `MAX_ORACLE_STALENESS_SECONDS` (300 seconds).

**To resume after investigating root cause:**
Click `Reset / Resume` in the dashboard or call `POST /api/v1/controls/resume`.

---

## 4. Oracle Watcher Health & Recovery

The `OracleUpdateWatcher` monitors push and pull price updates.
- If no price updates are received for $> 300$ seconds, it raises an `ORACLE_STALE` alert.
- Watcher failure runs in isolation and does **not** block periodic discovery scans.
- If the websocket or provider disconnects, the watcher attempts exponential backoff reconnection.

---

## 5. Key Rotation & Secret Management

1. Never store signing private keys in source control or plain text config files.
2. Store keys in environment variables or inject them via a secret manager (AWS Secrets Manager, HashiCorp Vault).
3. Rotate Google OAuth Client Secrets and Server `SECRET_KEY` every 90 days.
4. When rotating signer keys:
   - Deploy new key address to contract allowlist if applicable.
   - Update `SIGNER_PRIVATE_KEY` in environment.
   - Restart the service and verify on-chain native balance via `/api/v1/status`.
