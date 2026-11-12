"""System metrics, competition tracking, and event sink."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from liquidator.core.interfaces import EventSink
from pydantic import BaseModel


class CompetitionMetrics(BaseModel):
    """
    Observable competition signals (strictly labeled as observations per Section 8.5).
    """
    total_positions_detected: int = 0
    total_candidates_passed_simulation: int = 0
    bot_attempts_count: int = 0
    competitor_observed_attempts: int = 0
    opportunities_disappeared_before_inclusion: int = 0
    average_detection_to_inclusion_seconds: float = 0.0
    estimated_vs_realized_profit_ratio: float = 1.0


class EventHub(EventSink):
    """
    In-memory pub/sub broker broadcasting structured domain events
    to connected Server-Sent Events (SSE) clients and audit sinks.
    """

    def __init__(self):
        self._subscribers: List[asyncio.Queue] = []
        self.competition_metrics = CompetitionMetrics()
        self.total_events_emitted: int = 0

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        self.total_events_emitted += 1
        payload = {
            "type": event_type,
            "timestamp": time.time(),
            "data": data,
        }

        # Update competition observations
        if event_type == "opportunity_detected":
            self.competition_metrics.total_positions_detected += 1
        elif event_type == "simulation_passed":
            self.competition_metrics.total_candidates_passed_simulation += 1
        elif event_type in {"dry_run_attempt_completed", "live_attempt_finalized"}:
            self.competition_metrics.bot_attempts_count += 1

        # Broadcast to all live SSE client queues
        dead_queues = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                dead_queues.append(queue)
            except Exception:
                dead_queues.append(queue)

        for q in dead_queues:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)
