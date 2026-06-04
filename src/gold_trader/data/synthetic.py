from __future__ import annotations

from datetime import datetime, timedelta
from random import Random

from ..models import MarketBar


def generate_synthetic_bars(count: int = 500, seed: int = 7) -> list[MarketBar]:
    if count < 50:
        raise ValueError("count must be at least 50 to generate valid warmup history")

    rng = Random(seed)
    start_time = datetime(2024, 1, 1, 0, 0, 0)
    bars: list[MarketBar] = []
    price = 2_025.0
    scheduled_bias = 0.0
    bias_steps_remaining = 0

    for index in range(count):
        session = ("asia", "london", "new_york")[index % 3]
        baseline_drift = 0.08 if (index // 40) % 2 == 0 else -0.05
        noise = rng.uniform(-0.8, 0.8)
        bias = scheduled_bias if bias_steps_remaining > 0 else 0.0
        if bias_steps_remaining > 0:
            bias_steps_remaining -= 1
        else:
            scheduled_bias = 0.0

        open_price = price
        close_price = open_price + baseline_drift + bias + noise
        high = max(open_price, close_price) + rng.uniform(0.15, 0.75)
        low = min(open_price, close_price) - rng.uniform(0.15, 0.75)
        news_distance = 180.0 if index % 37 else 10.0
        spread = 0.18 + rng.uniform(0.02, 0.1)

        if index > 30 and index % 47 == 0:
            reference_low = min(bar.low for bar in bars[-20:])
            low = reference_low - rng.uniform(0.45, 1.25)
            close_price = reference_low + rng.uniform(0.25, 0.7)
            open_price = close_price - rng.uniform(0.1, 0.35)
            high = max(open_price, close_price) + rng.uniform(0.25, 0.6)
            session = "london"
            news_distance = 180.0
            scheduled_bias = 0.65
            bias_steps_remaining = 3
        elif index > 30 and index % 61 == 0:
            reference_high = max(bar.high for bar in bars[-20:])
            high = reference_high + rng.uniform(0.45, 1.25)
            close_price = reference_high - rng.uniform(0.25, 0.7)
            open_price = close_price + rng.uniform(0.1, 0.35)
            low = min(open_price, close_price) - rng.uniform(0.25, 0.6)
            session = "new_york"
            news_distance = 180.0
            scheduled_bias = -0.65
            bias_steps_remaining = 3

        bars.append(
            MarketBar(
                timestamp=start_time + timedelta(minutes=15 * index),
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=100 + rng.uniform(0.0, 25.0),
                spread=spread,
                session=session,
                news_distance_minutes=news_distance,
                dxy_close=102.0 + rng.uniform(-0.6, 0.6),
            )
        )
        price = close_price

    return bars