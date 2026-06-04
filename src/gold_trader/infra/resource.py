"""Resource governor — single source of truth for CPU/I-O budgets.

The user runs research jobs (holdout-eval, research-bundle, mine-patterns)
on a shared workstation.  Every parallel pool used to take its own ad-hoc
upper bound (14 here, 12 there, ``cpu_count()`` somewhere else), which
saturated the box during back-to-back research runs.

This module gives the whole codebase one knob:

    GOLD_MAX_WORKERS=N   (env var)
    --max-workers N      (every CLI subcommand respecting it)

Default budget = ``min(os.cpu_count() // 2, 8)`` — leaves a clear half of
the cores for the desktop, browsers, MT5/Wine, and the live agent itself.

Optional niceness knobs (Linux only, no-op elsewhere):

    GOLD_NICE=N          os.nice() applied at process start (default 10)
    GOLD_IONICE_CLASS=3  ionice class via subprocess wrapper if available

Pure stdlib.  Importing this module never lowers priority by itself;
callers explicitly invoke :func:`apply_niceness` when they want it (long
research jobs do; the live agent does not).
"""
from __future__ import annotations

import os


_DEFAULT_MAX_FRACTION = 0.5  # Use at most this fraction of logical cores.
_HARD_CAP = 8                # Never go above this regardless of cpu_count.


def cpu_budget(default: int | None = None) -> int:
    """Return the number of worker processes a research job may use.

    Resolution order:
      1. Explicit ``default`` argument (lets callers pass a CLI flag).
      2. ``GOLD_MAX_WORKERS`` env var.
      3. ``min(cpu_count // 2, _HARD_CAP)``.

    Always at least 1.
    """
    if default is not None and default > 0:
        return max(1, default)
    env = os.environ.get("GOLD_MAX_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    cpu = os.cpu_count() or 1
    return max(1, min(int(cpu * _DEFAULT_MAX_FRACTION), _HARD_CAP))


def resolve_workers(requested: int, task_count: int) -> int:
    """Bound a requested worker count by the global budget and task size.

    ``requested == 0`` means "use the global budget".  Any positive number
    is still capped by the global budget so a stray ``--max-workers 64``
    cannot pin the box.
    """
    budget = cpu_budget()
    if requested <= 0:
        n = budget
    else:
        n = min(requested, budget)
    return max(1, min(n, max(1, task_count)))


def apply_niceness() -> None:
    """Best-effort: lower scheduling priority for long research jobs.

    Reads ``GOLD_NICE`` (default 10).  Failures are silent — niceness is
    advisory, not load-bearing.
    """
    raw = os.environ.get("GOLD_NICE", "10")
    try:
        n = int(raw)
    except ValueError:
        return
    if n <= 0:
        return
    try:
        os.nice(n)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        # Windows / restricted env / already-niced.
        pass
