"""Cross-asset macro data: FRED (yields, VIX) + Stooq (DXY, SPX, USDJPY).

Pure stdlib.  All fetches are cached as CSV under ``data/macro/<name>.csv`` so
research runs are deterministic and offline-replayable.

Design notes
------------
* MarketBar is **not** modified.  Macro data is sidecar; strategies that need
  it accept a :class:`MacroFrame` in their constructor and look up values via
  ``as_of(ts)`` (last-known-value carry-forward — no lookahead).
* Daily resolution only.  Yields and VIX are daily series by definition.
  DXY/SPX/USDJPY are also daily here; intraday joins use the prior close,
  which is the correct discipline for an as-of join into intraday gold bars.
* Data quality: FRED uses ``.`` for missing values — skipped.  Stooq sometimes
  returns an HTML error page if rate-limited — we sniff and raise.
"""
from __future__ import annotations

import bisect
import csv
import io
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable

__all__ = [
    "MacroPoint",
    "MacroSeries",
    "MacroFrame",
    "MACRO_BUNDLE",
    "fetch_fred_series",
    "fetch_stooq_series",
    "load_or_fetch_macro",
    "sync_macro_bundle",
    "load_macro_frame",
    "write_macro_csv",
    "read_macro_csv",
]

_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}&coeked={end}"
_STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d&d1={start}&d2={end}"
_HTTP_TIMEOUT_SECONDS = 30
_USER_AGENT = "gold-trader/0.1 (+https://example.invalid; research)"

# Canonical bundle.  Names are stable identifiers used by strategies.
# Format: name -> (source, source_specific_id)
#
# All FRED.  We previously used Stooq for DXY/SPX/USDJPY but Stooq now requires
# a captcha-obtained API key for CSV downloads.  FRED carries equivalents that
# are arguably better for macro-regime work:
#   * DTWEXBGS is the Fed's trade-weighted broad dollar index (vs. the ICE DXY
#     which is 57.6% EUR-weighted); for gold-vs-dollar strength signals the
#     broad index is the more honest macro variable.
#   * SP500 = end-of-day S&P 500 close.
#   * DEXJPUS = Japan / U.S. exchange rate (JPY per USD).
MACRO_BUNDLE: dict[str, tuple[str, str]] = {
    "us10y":    ("fred", "DGS10"),     # 10-year Treasury yield, %
    "us2y":     ("fred", "DGS2"),      # 2-year Treasury yield, %
    "real10y":  ("fred", "DFII10"),    # 10-year TIPS (real) yield, %
    "vix":      ("fred", "VIXCLS"),    # CBOE VIX close
    "dxy":      ("fred", "DTWEXBGS"),  # Trade-weighted broad dollar index
    "spx":      ("fred", "SP500"),     # S&P 500 close
    "usdjpy":   ("fred", "DEXJPUS"),   # JPY per USD
}


@dataclass(frozen=True)
class MacroPoint:
    """One observation of a macro series.  Timestamp is 00:00 UTC of the date."""

    timestamp: datetime
    value: float


@dataclass
class MacroSeries:
    """A timestamped series with as-of and change helpers.

    Points must be sorted ascending by timestamp.  Constructor enforces this.
    """

    name: str
    source: str
    points: list[MacroPoint] = field(default_factory=list)

    def __post_init__(self) -> None:
        if any(self.points[i].timestamp > self.points[i + 1].timestamp for i in range(len(self.points) - 1)):
            self.points = sorted(self.points, key=lambda p: p.timestamp)
        # Cache timestamps for binary search.
        self._times: list[datetime] = [p.timestamp for p in self.points]

    def as_of(self, ts: datetime) -> float | None:
        """Last value with timestamp <= *ts*.  None if before first point."""
        if not self.points:
            return None
        pos = bisect.bisect_right(self._times, ts) - 1
        if pos < 0:
            return None
        return self.points[pos].value

    def change(self, ts: datetime, lookback_days: int) -> float | None:
        """Absolute change: ``as_of(ts) - as_of(ts - lookback_days)``.

        Both lookups are last-known-value, so weekends/holidays are handled
        naturally (the prior trading day is used).
        """
        if lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        now = self.as_of(ts)
        if now is None:
            return None
        prior_ts = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        prior_ts = prior_ts - timedelta(days=lookback_days)
        prior = self.as_of(prior_ts)
        if prior is None:
            return None
        return now - prior

    def pct_change(self, ts: datetime, lookback_days: int) -> float | None:
        """Relative change as a fraction (e.g., 0.012 = +1.2%)."""
        delta = self.change(ts, lookback_days)
        if delta is None:
            return None
        prior = self.as_of(ts.replace(hour=0, minute=0, second=0, microsecond=0)) or 0.0
        # Use the same prior anchor as change()
        from datetime import timedelta
        prior_ts = ts.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=lookback_days)
        prior = self.as_of(prior_ts)
        if prior is None or prior == 0.0:
            return None
        return delta / prior


@dataclass
class MacroFrame:
    """Container of named MacroSeries with a convenience ``get`` method."""

    series: dict[str, MacroSeries] = field(default_factory=dict)

    def get(self, name: str) -> MacroSeries | None:
        return self.series.get(name)

    def require(self, name: str) -> MacroSeries:
        s = self.series.get(name)
        if s is None:
            raise KeyError(f"Macro series '{name}' not loaded")
        return s

    def __contains__(self, name: str) -> bool:  # pragma: no cover — trivial
        return name in self.series

    def names(self) -> list[str]:  # pragma: no cover — trivial
        return list(self.series.keys())


# ---------- HTTP helpers ----------------------------------------------------


def _http_get(url: str) -> bytes:
    """GET with a polite User-Agent and a sane timeout.  Raises on non-200."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/csv,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            return resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover — network path
        raise RuntimeError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover — network path
        raise RuntimeError(f"Network error fetching {url}: {exc.reason}") from exc


def _date_to_utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


# ---------- FRED ------------------------------------------------------------


def fetch_fred_series(series_id: str, start: date, end: date) -> MacroSeries:
    """Fetch a FRED daily series via the public CSV endpoint (no API key needed).

    Missing values (encoded as ``.`` by FRED) are skipped.  Returned points
    are timestamped at 00:00 UTC of the observation date.
    """
    url = _FRED_URL.format(series_id=series_id, start=start.isoformat(), end=end.isoformat())
    raw = _http_get(url).decode("utf-8")
    reader = csv.reader(io.StringIO(raw))
    header = next(reader, None)
    if header is None or len(header) < 2:
        raise RuntimeError(f"Unexpected FRED response for {series_id}: empty body")
    # FRED header is ['DATE', '<SERIES_ID>'] (sometimes lower-case 'observation_date').
    points: list[MacroPoint] = []
    for row in reader:
        if len(row) < 2:
            continue
        date_str, value_str = row[0].strip(), row[1].strip()
        if value_str == "." or not value_str:
            continue
        try:
            obs_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            value = float(value_str)
        except ValueError:
            continue
        points.append(MacroPoint(timestamp=_date_to_utc_midnight(obs_date), value=value))
    if not points:
        raise RuntimeError(f"FRED returned no usable rows for {series_id} in {start}..{end}")
    return MacroSeries(name=series_id, source="fred", points=points)


# ---------- Stooq -----------------------------------------------------------


def fetch_stooq_series(symbol: str, start: date, end: date) -> MacroSeries:
    """Fetch a Stooq daily series via the public CSV endpoint.

    Stooq returns CSV with columns ``Date,Open,High,Low,Close,Volume``.
    We keep the close.  If rate-limited, Stooq returns a one-line text body
    starting with 'No data' — we detect and raise.
    """
    url = _STOOQ_URL.format(
        symbol=symbol,
        start=start.strftime("%Y%m%d"),
        end=end.strftime("%Y%m%d"),
    )
    raw = _http_get(url).decode("utf-8", errors="replace")
    if raw.lstrip().lower().startswith("no data") or "<html" in raw.lower():
        raise RuntimeError(f"Stooq rejected request for {symbol} (rate-limited or unknown symbol)")
    reader = csv.reader(io.StringIO(raw))
    header = next(reader, None)
    if header is None or "Date" not in header:
        raise RuntimeError(f"Unexpected Stooq response for {symbol}: missing header")
    close_idx = header.index("Close")
    date_idx = header.index("Date")
    points: list[MacroPoint] = []
    for row in reader:
        if len(row) <= close_idx:
            continue
        try:
            obs_date = datetime.strptime(row[date_idx].strip(), "%Y-%m-%d").date()
            value = float(row[close_idx].strip())
        except ValueError:
            continue
        points.append(MacroPoint(timestamp=_date_to_utc_midnight(obs_date), value=value))
    if not points:
        raise RuntimeError(f"Stooq returned no usable rows for {symbol} in {start}..{end}")
    return MacroSeries(name=symbol, source="stooq", points=points)


# ---------- Cache I/O -------------------------------------------------------


def write_macro_csv(series: MacroSeries, path: str | Path) -> None:
    """Persist a series to CSV with header ``date,value``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "value"])
        for p in series.points:
            writer.writerow([p.timestamp.date().isoformat(), f"{p.value:.6f}"])


def read_macro_csv(path: str | Path, *, name: str, source: str) -> MacroSeries:
    """Read a series previously written by :func:`write_macro_csv`."""
    path = Path(path)
    points: list[MacroPoint] = []
    with path.open() as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != ["date", "value"]:
            raise ValueError(f"Bad macro CSV header in {path}: {header!r}")
        for row in reader:
            if len(row) < 2:
                continue
            try:
                obs_date = datetime.strptime(row[0].strip(), "%Y-%m-%d").date()
                value = float(row[1].strip())
            except ValueError:
                continue
            points.append(MacroPoint(timestamp=_date_to_utc_midnight(obs_date), value=value))
    return MacroSeries(name=name, source=source, points=points)


# ---------- High-level API --------------------------------------------------


def _cache_path(cache_dir: Path, name: str) -> Path:
    return Path(cache_dir) / f"{name}.csv"


def load_or_fetch_macro(
    name: str,
    *,
    start: date,
    end: date,
    cache_dir: str | Path,
    refresh: bool = False,
) -> MacroSeries:
    """Return the named series, fetching from the network only if needed.

    If the cache file exists and ``refresh`` is False, it is returned as-is.
    Otherwise the canonical source for the name (from MACRO_BUNDLE) is queried,
    written to cache, and returned.
    """
    if name not in MACRO_BUNDLE:
        raise KeyError(f"Unknown macro series '{name}'.  Known: {sorted(MACRO_BUNDLE)}")
    source, source_id = MACRO_BUNDLE[name]
    cache_dir = Path(cache_dir)
    path = _cache_path(cache_dir, name)

    if path.exists() and not refresh:
        return read_macro_csv(path, name=name, source=source)

    if source == "fred":
        series = fetch_fred_series(source_id, start, end)
    elif source == "stooq":
        series = fetch_stooq_series(source_id, start, end)
    else:  # pragma: no cover — guarded by MACRO_BUNDLE
        raise ValueError(f"Unsupported source {source!r} for {name}")

    # Re-tag with the canonical name (not the source id) so downstream code is
    # source-agnostic.
    series = MacroSeries(name=name, source=source, points=series.points)
    write_macro_csv(series, path)
    return series


def sync_macro_bundle(
    cache_dir: str | Path,
    *,
    start: date,
    end: date,
    refresh: bool = True,
    names: Iterable[str] | None = None,
) -> dict[str, str]:
    """Fetch all canonical series and return ``{name: 'ok' | error_message}``.

    ``refresh=True`` (default) re-downloads even when cache exists — this is
    what cron-driven syncs want.  Set False for a one-shot warm-up.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    targets = list(names) if names is not None else list(MACRO_BUNDLE.keys())
    status: dict[str, str] = {}
    for name in targets:
        try:
            load_or_fetch_macro(name, start=start, end=end, cache_dir=cache_dir, refresh=refresh)
            status[name] = "ok"
        except Exception as exc:  # noqa: BLE001 — surface the message
            status[name] = f"error: {exc}"
    return status


def load_macro_frame(
    cache_dir: str | Path,
    *,
    names: Iterable[str] | None = None,
) -> MacroFrame:
    """Load already-cached series into a MacroFrame.

    Series whose cache file is missing are silently skipped — caller can check
    ``frame.names()`` to see what is available.
    """
    cache_dir = Path(cache_dir)
    targets = list(names) if names is not None else list(MACRO_BUNDLE.keys())
    out: dict[str, MacroSeries] = {}
    for name in targets:
        path = _cache_path(cache_dir, name)
        if not path.exists():
            continue
        source = MACRO_BUNDLE.get(name, ("unknown", ""))[0]
        out[name] = read_macro_csv(path, name=name, source=source)
    return MacroFrame(series=out)
