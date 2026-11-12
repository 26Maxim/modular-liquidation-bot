"""Abstract base classes and typed interfaces for liquidation bot components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from liquidator.core.models import (
    AdapterStatus,
    AttemptOutcome,
    BorrowerPosition,
    ChainType,
    SimulationResult,
    Token,
)


class ChainAdapter(ABC):
    """Interface for low-level blockchain interaction, RPC reads, simulation, and submission."""

    @property
    @abstractmethod
    def chain_type(self) -> ChainType:
        """Return the target blockchain ecosystem."""
        pass

    @property
    @abstractmethod
    def status(self) -> AdapterStatus:
        """Return the integration maturity level."""
        pass

    @abstractmethod
    async def get_latest_block_number(self) -> int:
        """Return the current chain head block/slot."""
        pass

    @abstractmethod
    async def get_native_balance(self, address: str) -> int:
        """Return native coin balance (in base units)."""
        pass

    @abstractmethod
    async def estimate_gas(self, tx_params: Dict[str, Any]) -> int:
        """Estimate gas units for a given transaction."""
        pass

    @abstractmethod
    async def simulate_transaction(self, tx_params: Dict[str, Any]) -> SimulationResult:
        """Simulate a transaction against chain state without broadcasting."""
        pass

    @abstractmethod
    async def submit_transaction(self, signed_tx_payload: Any) -> str:
        """Broadcast a signed transaction and return the tx hash/signature."""
        pass

    @abstractmethod
    async def get_transaction_status(self, tx_hash: str) -> Tuple[AttemptOutcome, Dict[str, Any]]:
        """Query inclusion status, receipt, and finalized gas used."""
        pass


class LendingProtocolAdapter(ABC):
    """Interface for lending protocol position discovery, risk parameter reading, and call construction."""

    @property
    @abstractmethod
    def protocol_id(self) -> str:
        """Unique identifier (e.g., 'moonwell_base', 'kamino_solana')."""
        pass

    @property
    @abstractmethod
    def chain(self) -> ChainType:
        """Associated chain ecosystem."""
        pass

    @property
    @abstractmethod
    def status(self) -> AdapterStatus:
        """Maturity level of this protocol adapter."""
        pass

    @abstractmethod
    async def discover_positions(self) -> List[BorrowerPosition]:
        """Fetch active borrower positions from indexer, registry, or events."""
        pass

    @abstractmethod
    async def refresh_position(self, account_id: str) -> Optional[BorrowerPosition]:
        """Fetch the latest on-chain state for a specific borrower position."""
        pass

    @abstractmethod
    def calculate_allowable_repay_and_collateral(
        self, position: BorrowerPosition, debt_token: Token, collateral_token: Token
    ) -> Tuple[int, int]:
        """
        Calculate allowable debt repayment amount (accounting for close factor)
        and expected seize collateral amount (accounting for liquidation incentive).
        Returns: (repay_raw, seize_collateral_raw)
        """
        pass

    @abstractmethod
    def build_liquidation_call(
        self,
        borrower_address: str,
        repay_amount_raw: int,
        debt_asset: Token,
        collateral_asset: Token,
    ) -> Dict[str, Any]:
        """Generate protocol-specific call data or instruction for liquidation."""
        pass


class FundingAdapter(ABC):
    """Interface for flash loan and prefunded liquidity providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifier of the funding provider (e.g., 'balancer_v2_base')."""
        pass

    @property
    @abstractmethod
    def chain(self) -> ChainType:
        """Associated chain."""
        pass

    @abstractmethod
    async def get_available_liquidity(self, token: Token) -> int:
        """Query available flash liquidity in raw units."""
        pass

    @abstractmethod
    def calculate_fee(self, token: Token, amount_raw: int) -> int:
        """Compute flash loan fee/premium in token base units."""
        pass

    @abstractmethod
    def build_flash_loan_call(
        self,
        token: Token,
        amount_raw: int,
        receiver_address: str,
        callback_calldata: bytes,
    ) -> Dict[str, Any]:
        """Construct the flash loan invocation call."""
        pass


class CollateralRouteAdapter(ABC):
    """Interface for quoting and building collateral disposal swaps."""

    @property
    @abstractmethod
    def venue_name(self) -> str:
        """Identifier for the DEX venue (e.g. 'uniswap_v3', 'cetus')."""
        pass

    @property
    @abstractmethod
    def chain(self) -> ChainType:
        """Associated chain."""
        pass

    @abstractmethod
    async def get_quote(
        self,
        from_token: Token,
        to_token: Token,
        amount_in_raw: int,
    ) -> int:
        """
        Query on-chain quoter for exact amount received after price impact and swap fee.
        Must return exact output units (raw) rather than analytical spot estimation.
        """
        pass

    @abstractmethod
    def build_swap_call(
        self,
        from_token: Token,
        to_token: Token,
        amount_in_raw: int,
        min_amount_out_raw: int,
        recipient: str,
    ) -> Dict[str, Any]:
        """Construct the swap execution calldata / instruction."""
        pass


class EventSink(ABC):
    """Interface for publishing domain events to persistence, websockets, and monitoring."""

    @abstractmethod
    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Publish structured event."""
        pass
