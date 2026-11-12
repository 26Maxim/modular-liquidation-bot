# Guide: Adding a New Lending Protocol

This guide demonstrates how to implement and register a new lending protocol adapter without modifying core risk controls, invariant gates, or UI templates.

---

## 1. Overview of Protocol Isolation

All lending protocol integrations must implement the `LendingProtocolAdapter` abstract base class:

```python
class LendingProtocolAdapter(ABC):
    @property
    @abstractmethod
    def protocol_id(self) -> str: pass

    @property
    @abstractmethod
    def chain(self) -> ChainType: pass

    @property
    @abstractmethod
    def status(self) -> AdapterStatus: pass

    @abstractmethod
    async def discover_positions(self) -> List[BorrowerPosition]: pass

    @abstractmethod
    async def refresh_position(self, account_id: str) -> Optional[BorrowerPosition]: pass

    @abstractmethod
    def calculate_allowable_repay_and_collateral(
        self, position: BorrowerPosition, debt_token: Token, collateral_token: Token
    ) -> Tuple[int, int]: pass

    @abstractmethod
    def build_liquidation_call(
        self, borrower_address: str, repay_amount_raw: int, debt_asset: Token, collateral_asset: Token
    ) -> Dict[str, Any]: pass
```

---

## 2. Step-by-Step Implementation: Adding Spark Protocol (EVM / Aave v3 Fork)

### Step 2.1: Implement the Protocol Adapter
Create `backend/src/liquidator/protocols/spark_adapter.py`:

```python
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from liquidator.core.interfaces import LendingProtocolAdapter
from liquidator.core.models import AdapterStatus, BorrowerPosition, ChainType, Token
from liquidator.core.scaling import norm_to_raw, raw_to_norm

class SparkProtocolAdapter(LendingProtocolAdapter):
    def __init__(self, pool_address: str = "0xC13eac3B4F9EED480045113B7af00F7B5655Ece8"):
        self.pool_address = pool_address
        self._status = AdapterStatus.READ_ONLY

    @property
    def protocol_id(self) -> str:
        return "spark_ethereum"

    @property
    def chain(self) -> ChainType:
        return ChainType.EVM

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def discover_positions(self) -> List[BorrowerPosition]:
        # Connect to indexer or parse Borrow event logs
        return []

    async def refresh_position(self, account_id: str) -> Optional[BorrowerPosition]:
        return None

    def calculate_allowable_repay_and_collateral(
        self, position: BorrowerPosition, debt_token: Token, collateral_token: Token
    ) -> Tuple[int, int]:
        # Spark risk parameters: 50% close factor, 5% liquidation bonus
        close_factor = Decimal("0.5") if position.health_factor > Decimal("0.95") else Decimal("1.0")
        bonus = Decimal("1.05")

        debt_norm = raw_to_norm(position.debt_amount_raw, debt_token.decimals)
        repay_norm = debt_norm * close_factor
        repay_raw = norm_to_raw(repay_norm, debt_token.decimals)

        repay_usd = repay_norm * debt_token.price_usd
        seize_usd = repay_usd * bonus
        collateral_norm = seize_usd / collateral_token.price_usd
        collateral_raw = norm_to_raw(collateral_norm, collateral_token.decimals)

        # Cap seize at available collateral
        if collateral_raw > position.collateral_amount_raw:
            collateral_raw = position.collateral_amount_raw
            repay_raw = norm_to_raw(
                (raw_to_norm(collateral_raw, collateral_token.decimals) * collateral_token.price_usd / bonus)
                / debt_token.price_usd,
                debt_token.decimals,
            )

        return repay_raw, collateral_raw

    def build_liquidation_call(
        self, borrower_address: str, repay_amount_raw: int, debt_asset: Token, collateral_asset: Token
    ) -> Dict[str, Any]:
        return {
            "target": self.pool_address,
            "method": "liquidationCall",
            "params": [
                collateral_asset.address,
                debt_asset.address,
                borrower_address,
                repay_amount_raw,
                False,
            ],
        }
```

### Step 2.2: Register the Adapter in `app.py`
Add to `protocol_adapters` dictionary:

```python
protocol_adapters["spark_ethereum"] = SparkProtocolAdapter()
```

### Step 2.3: Verification Checklist
1. Unit tests covering close factor and liquidation bonus calculations.
2. Invariant verification via `OpportunityEvaluator`.
3. Read-only position indexing test against RPC/subgraph.
4. Capability matrix verification on `/api/v1/adapters`.
