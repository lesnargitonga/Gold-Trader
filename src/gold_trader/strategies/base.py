from __future__ import annotations

from datetime import timedelta
from typing import Protocol, Sequence

from ..models import MarketBar, TradeSignal

_MAX_GAP_FOR_LOOKBACK = timedelta(hours=4)


class Strategy(Protocol):
    name: str

    def warmup_bars(self) -> int:
        ...

    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        ...


def lookback_spans_gap(
    bars: Sequence[MarketBar],
    index: int,
    lookback: int,
    max_gap: timedelta = _MAX_GAP_FOR_LOOKBACK,
) -> bool:
    """Return True if any consecutive bar pair within the lookback window has a gap
    larger than *max_gap*.  Used to skip signals whose prior-high/low was set before
    a weekend close or overnight session break."""
    start = max(0, index - lookback)
    for i in range(start + 1, index + 1):
        if bars[i].timestamp - bars[i - 1].timestamp > max_gap:
            return True
    return False