"""Token → USD and per-issue cost caps.

Prices are list prices per 1M tokens (input, output) as of Sep 2026 from third-party listings
of Nebius Token Factory; override via PRICES if they change. They are estimates for budgeting,
not invoices.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from warden.runlog.events import EventLog
from warden.schemas import Phase

PRICES: dict[str, tuple[float, float]] = {
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
}
DEFAULT_PRICE = (1.00, 3.00)  # be pessimistic for unknown models


def usd_for(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICES.get(model, DEFAULT_PRICE)
    return round((prompt_tokens * pin + completion_tokens * pout) / 1_000_000, 6)


class CostCapExceededError(RuntimeError):
    def __init__(self, spent: float, cap: float, phase: Phase) -> None:
        self.spent, self.cap, self.phase = spent, cap, phase
        super().__init__(
            f"cost cap ${cap:.2f} reached at {phase.capitalize()}; stopping. Raise with --max-usd"
        )


@dataclass
class CostMeter:
    cap_usd: float
    spent_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)
    calls: int = 0
    log: EventLog | None = None

    def charge(self, model: str, prompt_tokens: int, completion_tokens: int, phase: Phase) -> float:
        usd = usd_for(model, prompt_tokens, completion_tokens)
        self.spent_usd = round(self.spent_usd + usd, 6)
        self.by_model[model] = round(self.by_model.get(model, 0.0) + usd, 6)
        self.calls += 1
        self.check(phase)
        return usd

    def check(self, phase: Phase) -> None:
        if self.spent_usd > self.cap_usd:
            self._cap_hit(phase)

    def _cap_hit(self, phase: Phase) -> None:
        """Record the cap event (if a log is wired) and raise. Never caught to continue."""
        if self.log is not None:
            self.log.emit(phase, "cost_cap", {"spent_usd": self.spent_usd, "cap_usd": self.cap_usd})
        raise CostCapExceededError(self.spent_usd, self.cap_usd, phase)
