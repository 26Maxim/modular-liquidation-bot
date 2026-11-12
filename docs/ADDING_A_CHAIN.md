# Guide: Adding a New Blockchain Network

This guide details the step-by-step process of adding support for a new blockchain network without altering core economic, invariant, or risk logic.

---

## 1. Overview of Chain Isolation

The architecture strictly isolates chain-specific RPC communication, fee structures, transaction models, and simulation mechanics behind the `ChainAdapter` interface.

```
liquidator/chains/
  ├── evm_adapter.py      # EVM networks (Base, Arbitrum, Ethereum)
  ├── solana_adapter.py   # Solana SVM (Slot & Compute Budget model)
  ├── sui_adapter.py      # Sui Move (Checkpoints & PTB model)
  ├── aptos_adapter.py    # Aptos Move (Ledger versions & Entry functions)
  └── [new_chain].py      # Your new chain adapter
```

---

## 2. Step-by-Step Implementation: Adding Berachain / Monad (EVM Example)

### Step 2.1: Define Chain Enum Value
Add the new chain type to `liquidator.core.models.ChainType`:

```python
class ChainType(str, Enum):
    EVM = "EVM"
    SOLANA = "SOLANA"
    SUI = "SUI"
    APTOS = "APTOS"
    BERACHAIN = "BERACHAIN"  # New chain
```

### Step 2.2: Implement the Chain Adapter
Create `liquidator/chains/berachain_adapter.py`:

```python
from typing import Any, Dict, Tuple
from liquidator.core.interfaces import ChainAdapter
from liquidator.core.models import (
    AdapterStatus,
    AttemptOutcome,
    ChainType,
    SimulationResult,
)
from web3 import AsyncWeb3, AsyncHTTPProvider

class BerachainAdapter(ChainAdapter):
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self._status = AdapterStatus.READ_ONLY

    @property
    def chain_type(self) -> ChainType:
        return ChainType.BERACHAIN

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def get_latest_block_number(self) -> int:
        return await self.w3.eth.block_number

    async def get_native_balance(self, address: str) -> int:
        return await self.w3.eth.get_balance(self.w3.to_checksum_address(address))

    async def estimate_gas(self, tx_params: Dict[str, Any]) -> int:
        return await self.w3.eth.estimate_gas(tx_params)

    async def simulate_transaction(self, tx_params: Dict[str, Any]) -> SimulationResult:
        try:
            res = await self.w3.eth.call(tx_params)
            gas = await self.estimate_gas(tx_params)
            return SimulationResult(success=True, gas_used=gas, return_data=res.hex())
        except Exception as e:
            return SimulationResult(success=False, error_message=str(e))

    async def submit_transaction(self, signed_tx_payload: Any) -> str:
        # Fails closed until live execution is implemented and tested
        if self._status != AdapterStatus.EXECUTION_IMPLEMENTED:
            raise NotImplementedError("Live execution not implemented for Berachain.")
        return (await self.w3.eth.send_raw_transaction(signed_tx_payload)).hex()

    async def get_transaction_status(self, tx_hash: str) -> Tuple[AttemptOutcome, Dict[str, Any]]:
        receipt = await self.w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return AttemptOutcome.PENDING, {}
        return (
            AttemptOutcome.SUCCESS if receipt.get("status") == 1 else AttemptOutcome.REVERTED,
            {"gas_used": receipt.get("gasUsed", 0)},
        )
```

### Step 2.3: Register in Application Composition Root
In `liquidator/app.py`:

```python
chain_adapters[ChainType.BERACHAIN] = BerachainAdapter(rpc_url=settings.BERACHAIN_RPC_URL)
```

### Step 2.4: Update Capability Status
Start adapter status at `READ_ONLY` or `RESEARCHED`. Only graduate to `EXECUTION_IMPLEMENTED` after:
1. Smart contract deployment and verified source code.
2. Adversarial Foundry/unit tests passing against a local fork.
3. Live simulation verification.
