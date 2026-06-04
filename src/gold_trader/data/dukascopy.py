from __future__ import annotations

import csv
import lzma
import os
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..models import MarketBar

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_RETRIES = 6
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://freeserv.dukascopy.com/",
    "Accept": "*/*",
}
DEFAULT_PRICE_DECIMALS = {"XAUUSD": 3, "EURUSD": 5, "USDJPY": 3, "GBPUSD": 5, "USDCAD": 5, "USDCHF": 5}


@dataclass(frozen=True)
class Tick:
    timestamp: datetime
    bid: float
    ask: float
    bid_volume: float
    ask_volume: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def total_volume(self) -> float:
        return self.bid_volume + self.ask_volume


def download_dukascopy_bars(
    symbol: str,
    start_date: date,
    end_date: date,
    interval_minutes: int = 15,
    max_workers: int = 1,
    price_decimals: int | None = None,
) -> list[MarketBar]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    normalized_symbol = symbol.upper()
    decimals = price_decimals
    if decimals is None:
        decimals = DEFAULT_PRICE_DECIMALS.get(normalized_symbol)
    if decimals is None:
        raise ValueError("price_decimals is required for unsupported symbols")

    hours = list(_iter_hour_starts(start_date, end_date))
    if max_workers == 1:
        hourly_ticks = [
            _download_hour_ticks(normalized_symbol, hour_start, decimals)
            for hour_start in hours
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            hourly_ticks = list(
                executor.map(
                    _download_hour_ticks_worker,
                    ((normalized_symbol, hour_start, decimals) for hour_start in hours),
                )
            )

    return aggregate_ticks_to_bars(
        (tick for ticks_for_hour in hourly_ticks for tick in ticks_for_hour),
        interval_minutes=interval_minutes,
    )


def decode_dukascopy_ticks(
    payload: bytes,
    base_hour: datetime,
    price_decimals: int,
) -> list[Tick]:
    normalized_hour = _ensure_utc(base_hour)
    decompressed = lzma.decompress(payload)
    if len(decompressed) % 20 != 0:
        raise ValueError("unexpected Dukascopy payload size")

    price_divisor = 10**price_decimals
    ticks: list[Tick] = []
    for offset_millis, ask_raw, bid_raw, ask_volume, bid_volume in struct.iter_unpack(
        ">iiiff", decompressed
    ):
        ticks.append(
            Tick(
                timestamp=normalized_hour + timedelta(milliseconds=offset_millis),
                bid=bid_raw / price_divisor,
                ask=ask_raw / price_divisor,
                bid_volume=bid_volume,
                ask_volume=ask_volume,
            )
        )
    return ticks


def aggregate_ticks_to_bars(
    ticks: Iterable[Tick],
    interval_minutes: int,
) -> list[MarketBar]:
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")

    bars: list[MarketBar] = []
    current: _BarAccumulator | None = None

    for tick in ticks:
        bucket_start = _bucket_start(tick.timestamp, interval_minutes)
        if current is None or current.bucket_start != bucket_start:
            if current is not None:
                bars.append(current.to_market_bar(interval_minutes))
            current = _BarAccumulator.start(bucket_start, tick)
            continue
        current.update(tick)

    if current is not None:
        bars.append(current.to_market_bar(interval_minutes))

    return bars


def resample_bars(
    bars: Iterable[MarketBar],
    interval_minutes: int,
) -> list[MarketBar]:
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")

    resampled: list[MarketBar] = []
    current: _ResampledBarAccumulator | None = None

    for bar in bars:
        bucket_start = _bucket_start(bar.timestamp, interval_minutes)
        if current is None or current.bucket_start != bucket_start:
            if current is not None:
                resampled.append(current.to_market_bar(interval_minutes))
            current = _ResampledBarAccumulator.start(bucket_start, bar)
            continue
        current.update(bar)

    if current is not None:
        resampled.append(current.to_market_bar(interval_minutes))

    return resampled


def write_bars_to_csv(bars: Iterable[MarketBar], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "spread",
                "session",
                "news_distance_minutes",
                "dxy_close",
            ]
        )
        for bar in bars:
            writer.writerow(
                [
                    bar.timestamp.isoformat(),
                    f"{bar.open:.5f}",
                    f"{bar.high:.5f}",
                    f"{bar.low:.5f}",
                    f"{bar.close:.5f}",
                    f"{bar.volume:.8f}",
                    f"{bar.spread:.5f}",
                    bar.session,
                    "" if bar.news_distance_minutes is None else bar.news_distance_minutes,
                    "" if bar.dxy_close is None else bar.dxy_close,
                ]
            )


_CSV_HEADER = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "spread",
    "session",
    "news_distance_minutes",
    "dxy_close",
]


def append_bars_to_csv(
    bars: Iterable[MarketBar],
    path: str | Path,
    after: datetime | None = None,
) -> int:
    """Append *bars* to *path*, creating the file (with header) if absent.

    Only bars whose timestamp is strictly after *after* are written.  If
    *after* is None the function appends all bars regardless of timestamp
    (duplicates are not checked — callers are responsible for passing a
    correct *after* value).

    Returns the number of rows actually written.
    """
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    needs_header = not output_path.exists() or output_path.stat().st_size == 0

    written = 0
    with output_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if needs_header:
            writer.writerow(_CSV_HEADER)
        for bar in bars:
            if after is not None and bar.timestamp <= after:
                continue
            writer.writerow(
                [
                    bar.timestamp.isoformat(),
                    f"{bar.open:.5f}",
                    f"{bar.high:.5f}",
                    f"{bar.low:.5f}",
                    f"{bar.close:.5f}",
                    f"{bar.volume:.8f}",
                    f"{bar.spread:.5f}",
                    bar.session,
                    "" if bar.news_distance_minutes is None else bar.news_distance_minutes,
                    "" if bar.dxy_close is None else bar.dxy_close,
                ]
            )
            written += 1
    return written


def merge_dxy_into_csv(
    xauusd_csv: str | Path,
    eurusd_csv: str | Path,
    output_csv: str | Path | None = None,
) -> int:
    """Merge a DXY proxy column into an existing XAUUSD CSV.

    The DXY proxy is derived from EURUSD as ``1.0 / eurusd_close``, then
    linearly scaled so that the first overlapping bar equals 100 (a neutral
    index baseline).  EUR accounts for ~57.6% of the real DXY so the
    correlation is ≥ 0.97 and the sign of any move is always correct.

    For each XAUUSD bar the function finds the EURUSD bar whose timestamp is
    closest but *not later* than the XAUUSD timestamp (last-known-value carry-
    forward).  If no EURUSD bar is available for a given XAUUSD bar the field
    stays ``None``.

    Parameters
    ----------
    xauusd_csv:
        Path to the XAUUSD bars CSV.  If *output_csv* is None this file is
        overwritten in-place.
    eurusd_csv:
        Path to a CSV of EURUSD bars at any timeframe (normally 15m or 60m).
    output_csv:
        Destination path.  Defaults to overwriting *xauusd_csv*.

    Returns
    -------
    int
        Number of XAUUSD bars that received a non-None dxy_close value.
    """
    from .csv_loader import load_bars_from_csv  # avoid circular at module level

    xauusd_bars = load_bars_from_csv(xauusd_csv)
    eurusd_bars = load_bars_from_csv(eurusd_csv)

    if not xauusd_bars or not eurusd_bars:
        raise ValueError("Both CSVs must contain at least one bar")

    # Build a sorted list of (timestamp, eurusd_close) for binary-search.
    eur_times = [b.timestamp for b in eurusd_bars]
    eur_closes = [b.close for b in eurusd_bars]

    # Scale factor: proxy[0] = 100  where proxy = 1 / eurusd
    # This normalises the index so comparisons across time are intuitive.
    scale = 100.0 * eur_closes[0]  # = 100 / (1 / eur_closes[0])

    import bisect

    filled = 0
    merged: list[MarketBar] = []
    for bar in xauusd_bars:
        # Find the rightmost EURUSD bar whose timestamp <= bar.timestamp.
        pos = bisect.bisect_right(eur_times, bar.timestamp) - 1
        if pos < 0:
            dxy = None
        else:
            dxy = round(scale / eur_closes[pos], 4)
            filled += 1
        merged.append(
            MarketBar(
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                spread=bar.spread,
                session=bar.session,
                news_distance_minutes=bar.news_distance_minutes,
                dxy_close=dxy,
            )
        )

    dest = Path(output_csv) if output_csv else Path(xauusd_csv)
    write_bars_to_csv(merged, dest)
    return filled


@dataclass
class _BarAccumulator:
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread_sum: float
    spread_count: int

    @classmethod
    def start(cls, bucket_start: datetime, tick: Tick) -> _BarAccumulator:
        return cls(
            bucket_start=bucket_start,
            open=tick.mid,
            high=tick.mid,
            low=tick.mid,
            close=tick.mid,
            volume=tick.total_volume,
            spread_sum=tick.spread,
            spread_count=1,
        )

    def update(self, tick: Tick) -> None:
        midpoint = tick.mid
        self.high = max(self.high, midpoint)
        self.low = min(self.low, midpoint)
        self.close = midpoint
        self.volume += tick.total_volume
        self.spread_sum += tick.spread
        self.spread_count += 1

    def to_market_bar(self, interval_minutes: int = 0) -> MarketBar:
        return MarketBar(
            timestamp=self.bucket_start,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            spread=self.spread_sum / self.spread_count,
            session=_session_for_timestamp(self.bucket_start, interval_minutes),
        )


@dataclass
class _ResampledBarAccumulator:
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread_sum: float
    spread_count: int
    news_distance_minutes: float | None
    dxy_close: float | None

    @classmethod
    def start(cls, bucket_start: datetime, bar: MarketBar) -> _ResampledBarAccumulator:
        return cls(
            bucket_start=bucket_start,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            spread_sum=bar.spread,
            spread_count=1,
            news_distance_minutes=bar.news_distance_minutes,
            dxy_close=bar.dxy_close,
        )

    def update(self, bar: MarketBar) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.close = bar.close
        self.volume += bar.volume
        self.spread_sum += bar.spread
        self.spread_count += 1
        self.news_distance_minutes = _combine_news_distance(
            self.news_distance_minutes,
            bar.news_distance_minutes,
        )
        if bar.dxy_close is not None:
            self.dxy_close = bar.dxy_close

    def to_market_bar(self, interval_minutes: int = 0) -> MarketBar:
        return MarketBar(
            timestamp=self.bucket_start,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            spread=self.spread_sum / self.spread_count,
            session=_session_for_timestamp(self.bucket_start, interval_minutes),
            news_distance_minutes=self.news_distance_minutes,
            dxy_close=self.dxy_close,
        )


def _download_hour_ticks_worker(payload: tuple[str, datetime, int]) -> list[Tick]:
    symbol, hour_start, price_decimals = payload
    return _download_hour_ticks(symbol, hour_start, price_decimals)


def _download_hour_ticks(symbol: str, hour_start: datetime, price_decimals: int) -> list[Tick]:
    url = _dukascopy_url(symbol, hour_start)
    last_error: Exception | None = None

    for attempt in range(1, DEFAULT_RETRIES + 1):
        try:
            payload = _fetch_payload(url)
            if payload is None:
                return []
            return decode_dukascopy_ticks(payload, hour_start, price_decimals)
        except lzma.LZMAError as exc:
            last_error = exc
            curl_payload = _fetch_payload_with_curl(url)
            if curl_payload is not None:
                try:
                    return decode_dukascopy_ticks(curl_payload, hour_start, price_decimals)
                except lzma.LZMAError as curl_exc:
                    last_error = curl_exc
        except RuntimeError as exc:
            last_error = exc
            curl_payload = _fetch_payload_with_curl(url)
            if curl_payload is not None:
                try:
                    return decode_dukascopy_ticks(curl_payload, hour_start, price_decimals)
                except lzma.LZMAError as curl_exc:
                    last_error = curl_exc
        except OSError as exc:
            # socket.timeout / TimeoutError that escaped urllib's wrapping.
            last_error = exc
            curl_payload = _fetch_payload_with_curl(url)
            if curl_payload is not None:
                try:
                    return decode_dukascopy_ticks(curl_payload, hour_start, price_decimals)
                except lzma.LZMAError as curl_exc:
                    last_error = curl_exc

        if attempt < DEFAULT_RETRIES:
            time.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))

    if os.environ.get("GOLD_DUKASCOPY_SKIP_FAILED", "0") == "1":
        print(
            f"[dukascopy] WARN skipping hour after {DEFAULT_RETRIES} retries: {url} ({last_error})",
            file=sys.stderr,
            flush=True,
        )
        return []
    raise RuntimeError(f"failed to decode Dukascopy payload from {url}: {last_error}")


def _fetch_payload(url: str) -> bytes | None:
    last_error: Exception | None = None

    for attempt in range(1, DEFAULT_RETRIES + 1):
        request = Request(url, headers=DEFAULT_HEADERS)
        try:
            with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                payload = response.read()
                if not payload:
                    return None
                if payload.startswith(b"<html"):
                    raise RuntimeError("received HTML payload instead of binary data")
                return payload
        except HTTPError as exc:
            if exc.code == 404:
                return None
            last_error = exc
        except (RuntimeError, URLError, OSError) as exc:
            # OSError covers socket.timeout / TimeoutError raised mid-read.
            last_error = exc

        if attempt < DEFAULT_RETRIES:
            time.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))

    raise RuntimeError(f"failed to download Dukascopy payload from {url}: {last_error}")


def _fetch_payload_with_curl(url: str) -> bytes | None:
    curl_path = shutil.which("curl")
    if curl_path is None:
        return None

    command = [
        curl_path,
        "-fsSL",
        "-A",
        DEFAULT_HEADERS["User-Agent"],
        "-e",
        DEFAULT_HEADERS["Referer"],
        "--connect-timeout",
        "20",
        "--max-time",
        str(DEFAULT_TIMEOUT_SECONDS),
        url,
    ]
    result = subprocess.run(command, capture_output=True, text=False, check=False)
    if result.returncode != 0:
        return None
    if not result.stdout:
        return None
    if result.stdout.startswith(b"<html"):
        return None
    return result.stdout


def _dukascopy_url(symbol: str, hour_start: datetime) -> str:
    normalized_hour = _ensure_utc(hour_start)
    return (
        "https://datafeed.dukascopy.com/datafeed/"
        f"{symbol}/{normalized_hour.year}/{normalized_hour.month - 1:02d}/"
        f"{normalized_hour.day:02d}/{normalized_hour.hour:02d}h_ticks.bi5"
    )


def _iter_hour_starts(start_date: date, end_date: date) -> Iterable[datetime]:
    current = datetime.combine(start_date, dt_time(0, 0, tzinfo=timezone.utc))
    end_exclusive = datetime.combine(end_date + timedelta(days=1), dt_time(0, 0, tzinfo=timezone.utc))
    while current < end_exclusive:
        yield current
        current += timedelta(hours=1)


def _bucket_start(timestamp: datetime, interval_minutes: int) -> datetime:
    normalized = _ensure_utc(timestamp)
    day_start = normalized.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_day_start = normalized.hour * 60 + normalized.minute
    bucket_minutes = (minutes_since_day_start // interval_minutes) * interval_minutes
    return day_start + timedelta(minutes=bucket_minutes)


def _session_for_timestamp(timestamp: datetime, interval_minutes: int = 0) -> str:
    # Bars that span a full UTC day (or longer) cannot meaningfully be
    # assigned to a single session; label them "all_day" so intraday session
    # filters correctly skip them. The previous behaviour labelled every
    # 1440m bar "asia" because the bucket_start is 00:00 UTC.
    if interval_minutes >= 1440:
        return "all_day"
    hour = _ensure_utc(timestamp).hour
    if 13 <= hour < 21:
        return "new_york"
    if 7 <= hour < 13:
        return "london"
    return "asia"


def _ensure_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _combine_news_distance(current: float | None, incoming: float | None) -> float | None:
    if current is None:
        return incoming
    if incoming is None:
        return current
    return min(current, incoming)