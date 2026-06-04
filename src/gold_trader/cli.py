from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import os
import time
from pathlib import Path

from .backtest import summarize_backtest
from .backtest.engine import run_backtest
from .data import (
    append_bars_to_csv,
    download_dukascopy_bars,
    generate_synthetic_bars,
    load_bars_from_csv,
    read_last_bar_timestamp,
    resample_bars,
    write_bars_to_csv,
)
from .models import BacktestConfig, Side
from .paper import (
    PaperState,
    force_close_open_position,
    load_paper_state,
    monitor_open_position,
    open_position_from_decision,
    save_paper_state,
)
from .research import (
    analyze_timeframe_bundle,
    build_bundle_snapshot,
    build_liquidity_sweep_grid,
    default_asian_range_grid,
    default_compression_grid,
    default_liquidity_grid,
    default_london_breakout_grid,
    default_momentum_burst_grid,
    default_ny_session_breakout_grid,
    default_trend_pullback_grid,
    load_timeframe_bundle,
    run_liquidity_sweep_sweep,
    run_research_bundle,
    write_bundle_analysis_report,
)
from .research.calibration import calibrate_score_system
from .research.holdout import run_holdout_evaluation
from .research.permutation import run_permutation_test
from .strategies import (
    AsianRangeBreakoutStrategy,
    CompressionBreakoutStrategy,
    LiquiditySweepStrategy,
    LondonBreakoutStrategy,
    MomentumBurstStrategy,
    NYSessionBreakoutStrategy,
    TrendPullbackStrategy,
)
from .validation import run_walk_forward


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Long research jobs voluntarily lower their scheduling priority so
    # the desktop / live agent / browser stay responsive.  Live commands
    # must NOT do this.
    _RESEARCH_COMMANDS = {
        "holdout-eval", "research-bundle", "sweep", "walk-forward",
        "permutation-test", "mine-patterns", "mine-all",
    }
    if getattr(args, "command", None) in _RESEARCH_COMMANDS:
        try:
            from .infra.resource import apply_niceness
            apply_niceness()
        except Exception:  # pragma: no cover — best-effort
            pass

    if args.command == "smoke":
        bars = generate_synthetic_bars(count=args.bars, seed=args.seed)
        strategy = LiquiditySweepStrategy()
        summary = summarize_backtest(run_backtest(bars, strategy, BacktestConfig()))
        print(_format_summary(summary, f"Synthetic smoke run with {len(bars)} bars"))
        return

    if args.command == "download-dukascopy":
        bars = download_dukascopy_bars(
            symbol=args.symbol,
            start_date=_parse_date(args.start_date),
            end_date=_parse_date(args.end_date),
            interval_minutes=args.interval_minutes,
            max_workers=args.max_workers,
            price_decimals=args.price_decimals,
        )
        write_bars_to_csv(bars, args.output)
        print(f"source: dukascopy")
        print(f"symbol: {args.symbol.upper()}")
        print(f"bars_written: {len(bars)}")
        print(f"output: {args.output}")
        if bars:
            print(f"first_bar: {bars[0].timestamp.isoformat()}")
            print(f"last_bar: {bars[-1].timestamp.isoformat()}")
        return

    if args.command == "sync-dukascopy":
        end_date = _parse_date(args.end_date) if args.end_date else _utc_today()
        start_date = end_date - timedelta(days=args.days - 1)
        timeframes = _parse_int_list(args.timeframes)
        _validate_syncable_timeframes(args.base_interval_minutes, timeframes)

        base_bars = download_dukascopy_bars(
            symbol=args.symbol,
            start_date=start_date,
            end_date=end_date,
            interval_minutes=args.base_interval_minutes,
            max_workers=args.max_workers,
            price_decimals=args.price_decimals,
        )

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        symbol = args.symbol.upper()
        print(f"source: dukascopy")
        print(f"symbol: {symbol}")
        print(f"range: {start_date.isoformat()} to {end_date.isoformat()}")
        print(f"base_interval_minutes: {args.base_interval_minutes}")
        print(f"base_bars: {len(base_bars)}")

        for interval_minutes in sorted(set(timeframes)):
            timeframe_bars = base_bars
            if interval_minutes != args.base_interval_minutes:
                timeframe_bars = resample_bars(base_bars, interval_minutes)

            output_path = output_dir / _timeframe_filename(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval_minutes=interval_minutes,
            )
            write_bars_to_csv(timeframe_bars, output_path)
            print(f"timeframe_{interval_minutes}m_bars: {len(timeframe_bars)}")
            print(f"timeframe_{interval_minutes}m_output: {output_path}")

        return

    if args.command == "csv":
        bars = load_bars_from_csv(args.csv_path)
        strategy = LiquiditySweepStrategy()
        summary = summarize_backtest(run_backtest(bars, strategy, BacktestConfig()))
        print(_format_summary(summary, f"CSV backtest for {Path(args.csv_path).name}"))
        return

    if args.command == "walk-forward":
        bars = load_bars_from_csv(args.csv_path)
        results = run_walk_forward(
            bars=bars,
            strategy_factory=LiquiditySweepStrategy,
            config=BacktestConfig(),
            train_size=args.train_size,
            test_size=args.test_size,
            step_size=args.step_size,
        )
        if not results:
            print("No walk-forward windows were generated. Increase data or reduce window sizes.")
            return
        for result in results:
            window = result.window
            print(
                _format_summary(
                    result.summary,
                    (
                        f"Walk-forward window train[{window.train_start}:{window.train_end}] "
                        f"test[{window.test_start}:{window.test_end}]"
                    ),
                )
            )
        return

    if args.command == "sweep":
        bars = load_bars_from_csv(args.csv_path)
        parameter_grid = build_liquidity_sweep_grid(
            lookbacks=_parse_int_list(args.lookbacks),
            atr_periods=_parse_int_list(args.atr_periods),
            min_sweep_atrs=_parse_float_list(args.min_sweep_atrs),
            risk_rewards=_parse_float_list(args.risk_rewards),
            max_spreads=_parse_float_list(args.max_spreads),
            min_news_distances=_parse_float_list(args.min_news_distances),
        )
        results = run_liquidity_sweep_sweep(
            bars=bars,
            config=BacktestConfig(),
            parameter_grid=parameter_grid,
            max_workers=args.max_workers,
        )
        print(f"evaluated_combinations: {len(results)}")
        for rank, result in enumerate(results[: args.top], start=1):
            params = result.parameters
            print(
                _format_summary(
                    result.summary,
                    (
                        f"Rank {rank} lookback={params.lookback} atr_period={params.atr_period} "
                        f"min_sweep_atr={params.min_sweep_atr} risk_reward={params.risk_reward} "
                        f"max_spread={params.max_spread} min_news_distance={params.min_news_distance_minutes}"
                    ),
                )
            )
        return

    if args.command == "research-bundle":
        datasets = load_timeframe_bundle(args.data_dir, _parse_int_list(args.timeframes))
        if not datasets:
            print("No synchronized timeframe CSVs were found for the requested timeframes.")
            return

        results = run_research_bundle(
            datasets=datasets,
            config=BacktestConfig(),
            families=_parse_family_list(args.families),
            liquidity_grid=default_liquidity_grid(),
            compression_grid=default_compression_grid(),
            asian_range_grid=default_asian_range_grid(),
            london_breakout_grid=default_london_breakout_grid(),
            trend_pullback_grid=default_trend_pullback_grid(),
            ny_session_breakout_grid=default_ny_session_breakout_grid(),
            momentum_burst_grid=default_momentum_burst_grid(),
            train_bars=args.train_bars,
            test_bars=args.test_bars,
            step_bars=args.step_bars,
            min_trades=args.min_trades,
            max_workers=args.max_workers,
        )
        if not results:
            print("No research candidates met the minimum trade threshold.")
            return

        print(f"datasets_loaded: {','.join(str(timeframe) for timeframe in sorted(datasets))}")
        print(f"research_results: {len(results)}")
        for rank, result in enumerate(results[: args.top], start=1):
            print(
                _format_research_result(
                    rank=rank,
                    result=result,
                )
            )
        return

    if args.command == "analyze-bundle":
        datasets = load_timeframe_bundle(args.data_dir, _parse_int_list(args.timeframes))
        if not datasets:
            print("No synchronized timeframe CSVs were found for the requested timeframes.")
            return

        analysis = analyze_timeframe_bundle(datasets)
        report_path = write_bundle_analysis_report(
            datasets=datasets,
            analysis=analysis,
            output_dir=args.output_dir,
            include_charts=not args.skip_charts,
        )
        print(f"datasets_loaded: {','.join(str(timeframe) for timeframe in sorted(datasets))}")
        print(f"alignment: {analysis.alignment_label}")
        print(f"report: {report_path}")
        if not args.skip_charts:
            print(f"charts_dir: {report_path.parent / 'charts'}")
        for profile in analysis.profiles:
            print(
                _format_analysis_profile(profile)
            )
        return

    if args.command == "monitor-bundle":
        # Load macro frame once for the whole monitor session — read-only.
        _monitor_macro = None
        try:
            from .data.macro import load_macro_frame as _load_mf_mon
            _monitor_macro = _load_mf_mon(Path("data/macro"))
            if not _monitor_macro.names():
                _monitor_macro = None
        except Exception:
            _monitor_macro = None
        for iteration in range(args.iterations):
            datasets = load_timeframe_bundle(args.data_dir, _parse_int_list(args.timeframes))
            if not datasets:
                print("No synchronized timeframe CSVs were found for the requested timeframes.")
                return

            snapshot = build_bundle_snapshot(
                datasets=datasets,
                families=_parse_family_list(args.families),
                max_candidates=args.max_candidates,
                macro_frame=_monitor_macro,
            )
            print(_format_bundle_snapshot(snapshot, iteration + 1))
            if iteration + 1 < args.iterations:
                time.sleep(args.interval_seconds)
        return

    if args.command == "agent-cycle":
        timeframes = _parse_int_list(args.timeframes)
        _validate_syncable_timeframes(args.base_interval_minutes, timeframes)
        paper_state_path = Path(args.output_dir) / "paper_state.json"
        state = load_paper_state(paper_state_path, starting_equity=args.paper_equity)

        # ------------------------------------------------------------------
        # Production infrastructure: structured logging, SQLite state DB,
        # event bus, fills ledger.  These are best-effort — if anything
        # fails we still want the agent loop to run on the legacy paths.
        # ------------------------------------------------------------------
        from .infra import (
            configure_logging,
            get_logger,
            open_state_db,
            EventBus,
            EventKind,
            sync_fills_ledger,
            EquityGuardConfig,
            DivergenceConfig,
            evaluate_equity_guard,
            evaluate_divergence_guard,
            evaluate_tick_age,
            trip_kill_switch,
        )
        try:
            configure_logging(log_dir=Path("logs"), level="INFO")
            _infra_log = get_logger("gold_trader.agent")
            _infra_db = open_state_db(Path(args.output_dir) / "state.db")
            _infra_bus = EventBus(
                _infra_db, jsonl_path=Path("logs/events.jsonl"),
            )
            _correlation_id = _infra_bus.new_correlation_id()
            _infra_bus.publish(
                EventKind.AGENT_CYCLE_STARTED,
                {
                    "output_dir": str(args.output_dir),
                    "iterations": int(args.iterations),
                    "symbol": args.symbol,
                },
                correlation_id=_correlation_id,
            )
        except Exception as _exc:
            _infra_log = None
            _infra_db = None
            _infra_bus = None
            _correlation_id = None
            print(f"infra_init_failed: {type(_exc).__name__}: {_exc}")

        # ------------------------------------------------------------------
        # Broker routing: GOLD_BROKER selects paper (default) or live.
        # When live, agent-cycle places real orders via the broker abstraction
        # AND keeps the paper sim running in parallel for comparison.
        # ------------------------------------------------------------------
        from .live import (
            get_broker_from_env,
            BrokerError as _BrokerError,
            OrderRequest as _OrderRequest,
            OrderSide as _OrderSide,
            load_live_state,
            save_live_state,
            LiveAgentState,
            serialize_closed_trade as _serialize_closed,
        )
        live_broker = None
        live_state_path = Path(args.output_dir) / "live_state.json"
        live_state = load_live_state(live_state_path)
        try:
            _broker_candidate = get_broker_from_env()
            _info = _broker_candidate.get_account_info()
            print(
                f"broker: {_broker_candidate.name} equity={_info.equity:.2f} "
                f"currency={_info.currency} leverage={_info.leverage:.0f}x"
            )
            if _broker_candidate.name != "paper":
                live_broker = _broker_candidate
        except _BrokerError as _exc:
            print(f"broker_unavailable: {_exc}")
            if _infra_bus is not None:
                _infra_bus.publish(
                    EventKind.BRIDGE_ERROR, {"error": str(_exc)},
                    correlation_id=_correlation_id,
                )
        except Exception as _exc:
            print(f"broker_query_error: {type(_exc).__name__}: {_exc}")

        # Persistent rolling CSV paths — one per timeframe, fixed names so
        # they accumulate data across every cron invocation instead of being
        # overwritten with a dated filename each time.
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        symbol = args.symbol.upper()
        base_tf = args.base_interval_minutes
        persistent_paths: dict[int, Path] = {
            tf: output_dir / f"{symbol.lower()}_{tf}m.csv"
            for tf in sorted(set(timeframes))
        }

        for iteration in range(args.iterations):
            # Reset kill switch at the start of each new UTC day
            state = state.with_daily_reset_if_needed()

            # Default-init flags used by the equity guard further down.
            _equity_guard_tripped = False

            end_date = _parse_date(args.end_date) if args.end_date else _utc_today()

            # --- Incremental fetch: only download bars we don't already have ---
            base_path = persistent_paths[base_tf]
            last_ts = read_last_bar_timestamp(base_path)

            if last_ts is not None:
                # We have existing data — only fetch from the day of the last
                # bar to today (Dukascopy serves whole days; dedup handles overlap).
                fetch_start = last_ts.date()
                print(f"incremental_fetch: last_bar={last_ts.isoformat()} fetching {fetch_start} to {end_date}")
            else:
                # No existing data — do a full backfill of --days days.
                fetch_start = end_date - timedelta(days=args.days - 1)
                print(f"full_fetch: no existing data, fetching {fetch_start} to {end_date}")

            base_bars = download_dukascopy_bars(
                symbol=args.symbol,
                start_date=fetch_start,
                end_date=end_date,
                interval_minutes=base_tf,
                max_workers=args.max_workers,
                price_decimals=args.price_decimals,
            )

            # Append new bars to each timeframe's persistent CSV, then load
            # the last --days worth of bars for strategy evaluation.
            datasets: dict[int, list] = {}
            new_bar_counts: dict[int, int] = {}
            for interval_minutes in sorted(set(timeframes)):
                timeframe_bars = base_bars
                if interval_minutes != base_tf:
                    timeframe_bars = resample_bars(base_bars, interval_minutes)
                p = persistent_paths[interval_minutes]
                last_ts_tf = read_last_bar_timestamp(p)
                added = append_bars_to_csv(timeframe_bars, p, after=last_ts_tf)
                new_bar_counts[interval_minutes] = added
                # Load only the last --days window for in-memory strategy use.
                lookback_start = end_date - timedelta(days=args.days - 1)
                all_bars = load_bars_from_csv(p)
                datasets[interval_minutes] = [
                    b for b in all_bars
                    if b.timestamp.date() >= lookback_start
                ]

            print(f"agent_cycle_iteration: {iteration + 1}")
            print(f"synced_range: {fetch_start.isoformat()} to {end_date.isoformat()}")
            print(f"new_bars: { {tf: n for tf, n in new_bar_counts.items()} }")
            print(f"loaded_bars: { {tf: len(b) for tf, b in datasets.items()} }")

            # ----------------------------------------------------------------
            # LIVE BROKER BRANCH — runs in parallel with paper sim.
            # Paper continues to execute below for shadow-comparison.
            # ----------------------------------------------------------------
            if live_broker is not None:
                live_state = live_state.with_daily_reset_if_needed()
                try:
                    live_position = live_broker.get_open_position()
                    live_pending = live_broker.get_pending_order()
                    live_account = live_broker.get_account_info()
                except _BrokerError as exc:
                    print(f"live_broker_error: {exc}")
                    live_position = None
                    live_pending = None
                    live_account = None

                # Detect a position that closed since the last cycle.
                if (
                    live_state.last_known_position_id is not None
                    and live_position is None
                    and live_pending is None
                ):
                    print(
                        f"live_position_closed: ticket={live_state.last_known_position_id} "
                        f"(SL/TP hit at broker side; ledger reconstruction "
                        f"requires deal history — see MT5 GUI for now)"
                    )
                    live_state.last_known_position_id = None

                if live_position is not None:
                    live_state.last_known_position_id = live_position.broker_order_id
                    print(
                        f"live_position_open: ticket={live_position.broker_order_id} "
                        f"side={live_position.side.value} units={live_position.units:.2f} "
                        f"entry={live_position.entry_price:.2f} "
                        f"stop={live_position.stop_price:.2f} "
                        f"target={live_position.target_price:.2f} "
                        f"upnl={live_position.unrealised_pnl:+.2f}"
                    )
                if live_pending is not None:
                    live_state.last_known_position_id = live_pending.broker_order_id
                    print(
                        f"live_pending_resting: ticket={live_pending.broker_order_id} "
                        f"side={live_pending.side.value} units={live_pending.units:.2f} "
                        f"entry={live_pending.entry_price:.2f} "
                        f"stop={live_pending.stop_price:.2f} "
                        f"target={live_pending.target_price:.2f}"
                    )
                if live_account is not None:
                    print(
                        f"live_account: equity={live_account.equity:.2f} "
                        f"trades_today={live_state.daily_trades_opened}/"
                        f"{args.max_daily_trades}"
                    )
                    # Equity snapshot for the dashboard / metrics endpoint.
                    if _infra_db is not None:
                        try:
                            _infra_db.insert_equity_snapshot({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "broker_name": live_broker.name,
                                "equity": float(live_account.equity),
                                "balance": float(live_account.balance),
                                "margin_used": float(live_account.margin_used),
                                "margin_free": float(live_account.margin_free),
                                "paper_equity": float(state.paper_equity),
                                "open_position_count": 1 if live_position else 0,
                                "pending_order_count": 1 if live_pending else 0,
                            })
                        except Exception as _exc:  # noqa: BLE001
                            print(f"equity_snapshot_failed: {_exc}")

                # Pull broker deal history into the fills ledger.  Idempotent.
                if _infra_db is not None and _infra_bus is not None:
                    try:
                        _ledger_result = sync_fills_ledger(
                            live_broker, _infra_db, _infra_bus,
                            magic=int(os.environ.get("GOLD_MAGIC", "20260507")),
                        )
                        if _ledger_result["new_deals"]:
                            print(
                                f"ledger_synced: new_deals="
                                f"{_ledger_result['new_deals']} "
                                f"new_round_trips={_ledger_result['new_round_trips']}"
                            )
                    except Exception as _exc:  # noqa: BLE001
                        print(f"ledger_sync_failed: {_exc}")

                # Default-init for the iteration: any guard may flip this.
                _equity_guard_tripped = False

                # ------------------------------------------------------
                # B4 — connectivity watchdog.  If the bridge tick feed
                # has gone silent for too long we cannot trust prices,
                # so we trip the kill switch (flatten + block new orders).
                # ------------------------------------------------------
                if _infra_db is not None and _infra_bus is not None:
                    try:
                        _healthz = getattr(live_broker, "healthz", None)
                        if callable(_healthz):
                            _h = _healthz() or {}
                            _tf = _h.get("tick_feed") if isinstance(_h, dict) else None
                            _age = (
                                _tf.get("last_tick_age_sec")
                                if isinstance(_tf, dict)
                                else None
                            )
                            _ta_threshold = float(
                                os.environ.get("GOLD_TICK_AGE_MAX_SEC", "300")
                            )
                            # Only enforce when the bridge actually exposes a feed.
                            if _tf is not None:
                                _ta = evaluate_tick_age(
                                    _age, threshold_sec=_ta_threshold,
                                )
                                if _ta.decision in ("stale", "missing"):
                                    print(
                                        f"tick_age_watchdog_trip: "
                                        f"decision={_ta.decision} reason={_ta.reason}"
                                    )
                                    from .infra.risk import EquityGuardVerdict as _EGV
                                    _synth = _EGV(
                                        decision="trip",
                                        reason=_ta.reason,
                                        equity=0.0,
                                        balance=0.0,
                                        session_start_equity=0.0,
                                        daily_pnl=0.0,
                                        daily_pnl_fraction=0.0,
                                        triggered_rule=f"tick_age_{_ta.decision}",
                                    )
                                    trip_kill_switch(
                                        live_broker, _infra_db, _infra_bus, _synth,
                                        magic=int(os.environ.get("GOLD_MAGIC", "20260507")),
                                        correlation_id=_correlation_id,
                                    )
                                    _equity_guard_tripped = True
                    except Exception as _exc:  # noqa: BLE001
                        print(f"tick_age_watchdog_failed: {_exc}")

                # ------------------------------------------------------
                # D1 — equity-guard kill switch.  Broker-truth driven.
                # Trips on daily drawdown / single-cycle drop / equity
                # floor.  Flattens the account and halts new entries.
                # ------------------------------------------------------
                if _infra_db is not None and _infra_bus is not None:
                    try:
                        _eg_cfg = EquityGuardConfig(
                            daily_loss_fraction=float(
                                os.environ.get(
                                    "GOLD_DAILY_LOSS_FRACTION", "0.04"
                                )
                            ),
                            min_absolute_equity=float(
                                os.environ.get(
                                    "GOLD_MIN_EQUITY", "0"
                                )
                            ),
                            max_single_cycle_loss_fraction=float(
                                os.environ.get(
                                    "GOLD_MAX_CYCLE_LOSS", "0.025"
                                )
                            ),
                        )
                        _eg = evaluate_equity_guard(
                            live_broker, _infra_db, config=_eg_cfg,
                        )
                        if _eg.decision == "trip":
                            print(
                                f"equity_guard_trip: rule={_eg.triggered_rule} "
                                f"reason={_eg.reason}"
                            )
                            _eq_report = trip_kill_switch(
                                live_broker, _infra_db, _infra_bus, _eg,
                                magic=int(os.environ.get("GOLD_MAGIC", "20260507")),
                                correlation_id=_correlation_id,
                            )
                            print(
                                f"equity_guard_flatten: "
                                f"closed={_eq_report.closed_positions} "
                                f"cancelled={_eq_report.cancelled_pending} "
                                f"errors={len(_eq_report.errors)}"
                            )
                            _equity_guard_tripped = True
                        elif _eg.decision == "warn":
                            print(f"equity_guard_warn: {_eg.reason}")
                    except Exception as _exc:  # noqa: BLE001
                        print(f"equity_guard_failed: {_exc}")

                # ------------------------------------------------------
                # D2 — broker-vs-paper divergence guard.  Alerts on drift
                # between broker realised P&L, fills-ledger sum, and
                # paper-sim equity.  Does not flatten — informational.
                # ------------------------------------------------------
                if _infra_db is not None and _infra_bus is not None:
                    try:
                        _div = evaluate_divergence_guard(
                            live_broker, _infra_db,
                            paper_equity=float(state.paper_equity),
                            paper_starting_equity=float(args.paper_equity),
                            magic=int(os.environ.get("GOLD_MAGIC", "20260507")),
                            config=DivergenceConfig(
                                absolute_tolerance=float(
                                    os.environ.get(
                                        "GOLD_DIVERGENCE_ABS_TOL", "5.0"
                                    )
                                ),
                                relative_tolerance=float(
                                    os.environ.get(
                                        "GOLD_DIVERGENCE_REL_TOL", "0.005"
                                    )
                                ),
                            ),
                        )
                        if _div.decision == "alert":
                            print(f"divergence_alert: {_div.reason}")
                            _infra_bus.publish(
                                EventKind.BRIDGE_ERROR,
                                {
                                    "kind": "divergence_alert",
                                    "broker_realised": _div.broker_realised_pnl,
                                    "ledger_realised": _div.ledger_realised_pnl,
                                    "paper_pnl": _div.paper_pnl,
                                    "reason": _div.reason,
                                },
                                correlation_id=_correlation_id,
                            )
                        elif _div.decision == "warn":
                            print(f"divergence_warn: {_div.reason}")
                    except Exception as _exc:  # noqa: BLE001
                        print(f"divergence_guard_failed: {_exc}")

                # Reconcile: if live broker is flat (no position, no pending)
                # but the paper sim still shows an open position, the live
                # trade closed externally (SL/TP hit, manual close).  Treat
                # the live broker as authoritative and close the paper
                # position at the latest available bar so the agent can
                # accept new signals on the next iteration.
                if (
                    live_position is None
                    and live_pending is None
                    and state.open_position is not None
                ):
                    latest_close = None
                    for tf_bars in datasets.values():
                        if tf_bars:
                            cand = tf_bars[-1]
                            if (
                                latest_close is None
                                or cand.timestamp > latest_close.timestamp
                            ):
                                latest_close = cand
                    if latest_close is not None:
                        state, recon_msg = force_close_open_position(
                            state,
                            exit_price=float(latest_close.close),
                            reason="live_reconcile",
                            risk_per_trade=args.risk_per_trade,
                        )
                        if recon_msg:
                            print(f"paper_trade_event: {recon_msg}")

            # --- paper state: monitor open position first ---
            if state.open_position is not None:
                flat_bars = sorted(
                    (bar for bars in datasets.values() for bar in bars),
                    key=lambda b: b.timestamp,
                )
                state, close_event = monitor_open_position(
                    state, flat_bars, risk_per_trade=args.risk_per_trade,
                )
                if close_event:
                    print(f"paper_trade_event: {close_event}")
                else:
                    print(
                        f"paper_trade_monitoring: position open "
                        f"side={state.open_position.side} "
                        f"entry={state.open_position.entry:.2f} "
                        f"stop={state.open_position.stop:.2f} "
                        f"target={state.open_position.target:.2f}"
                    )

            print(
                f"paper_equity: {state.paper_equity:.2f} "
                f"trades={state.total_trades} wr={state.win_rate:.0%} "
                f"daily_trades={state.daily_trades_opened}/{args.max_daily_trades} "
                f"kill_switch={state.kill_switch_triggered}"
            )

            if state.kill_switch_triggered:
                print("paper_kill_switch: daily drawdown exceeded 4% — agent halted")
                save_paper_state(state, paper_state_path)
                if live_broker is not None:
                    save_live_state(live_state, live_state_path)
                break

            # --- scan for new entry only if no position open and under daily trade limit ---
            at_daily_limit = state.daily_trades_opened >= args.max_daily_trades
            if state.open_position is None and not at_daily_limit:
                # Macro frame is required by `timed_horizon_macro_regime` and
                # used downstream by the macro decision filter.  Load once
                # per iteration; gracefully degrade if cache is missing.
                _macro_frame = None
                try:
                    from .data.macro import load_macro_frame as _load_mf
                    _macro_frame = _load_mf(Path("data/macro"))
                    if not _macro_frame.names():
                        _macro_frame = None
                except Exception as _exc:
                    print(f"macro_frame_unavailable: {type(_exc).__name__}: {_exc}")
                    _macro_frame = None

                snapshot = build_bundle_snapshot(
                    datasets=datasets,
                    families=_parse_family_list(args.families),
                    max_candidates=args.max_candidates,
                    macro_frame=_macro_frame,
                    market_levels_path=str(Path("config/market_levels.json")),
                    news_calendar_path=str(Path("data/macro/news_calendar.csv")),
                    shadow_journal_path=str(Path(args.output_dir) / "ifvg_shadow_setups.csv"),
                    openai_research_config_path=str(Path("config/openai_research.json")),
                    openai_research_cache_path=str(Path("data/cache/openai_market_research.json")),
                )
                print(_format_bundle_snapshot(snapshot, iteration + 1))

                # ----------------------------------------------------------
                # Macro decision filter (opt-in via GOLD_MACRO_FILTER env).
                # 'off' / unset: pass-through.
                # 'soft':        log verdict but never block.
                # 'hard':        skip orders the filter blocks.
                # ----------------------------------------------------------
                _macro_filter_mode = os.environ.get("GOLD_MACRO_FILTER", "off").lower()
                _macro_filter_block = False
                # ----------------------------------------------------------
                # News blackout (opt-in via GOLD_NEWS_BLACKOUT_MIN env var,
                # default 0 = disabled).  When >0, skip new orders within
                # that many minutes of a high-impact USD event listed in
                # data/macro/news_calendar.csv.  Logged either way.
                # ----------------------------------------------------------
                _news_blackout_min = float(
                    os.environ.get("GOLD_NEWS_BLACKOUT_MIN", "0") or 0
                )
                _news_block = False
                if _news_blackout_min > 0 and snapshot.decision.status == "accept":
                    try:
                        from .calendar import NewsCalendar
                        _cal = NewsCalendar.load(Path("data/macro/news_calendar.csv"))
                        _last_bar = next(iter(datasets.values()))[-1]
                        _hit, _ev = _cal.is_blackout(
                            _last_bar.timestamp,
                            window_minutes=_news_blackout_min,
                            min_impact="high",
                        )
                        if _hit and _ev is not None:
                            print(
                                f"news_blackout: BLOCK event='{_ev.event}' "
                                f"at {_ev.timestamp.isoformat()} "
                                f"window=±{_news_blackout_min:.0f}min"
                            )
                            _news_block = True
                        else:
                            print(
                                f"news_blackout: clear (window=±{_news_blackout_min:.0f}min)"
                            )
                    except Exception as _exc:  # pragma: no cover
                        print(
                            f"news_blackout_error: {type(_exc).__name__}: {_exc}"
                            " — pass-through"
                        )
                if _news_block:
                    _macro_filter_block = True
                if (
                    _macro_filter_mode in ("soft", "hard")
                    and snapshot.decision.status == "accept"
                    and snapshot.decision.side is not None
                ):
                    try:
                        from .data.macro import load_macro_frame
                        from .macro_filter import MacroDecisionFilter
                        _mf = load_macro_frame(Path("data/macro"))
                        if len(_mf.names()) > 0:
                            _filter = MacroDecisionFilter(macro=_mf)
                            _last_bar = next(iter(datasets.values()))[-1]
                            _verdict = _filter.evaluate(
                                snapshot.decision.side, _last_bar.timestamp,
                            )
                            print(
                                f"macro_filter[{_macro_filter_mode}]: "
                                f"verdict={_verdict.verdict} "
                                f"reason={_verdict.reason} "
                                f"tags={','.join(_verdict.regime_tags) or '-'}"
                            )
                            if (
                                _macro_filter_mode == "hard"
                                and _verdict.verdict == "block"
                            ):
                                _macro_filter_block = True
                            if _infra_bus is not None:
                                _infra_bus.publish(
                                    EventKind.AGENT_CYCLE_STARTED,
                                    {
                                        "macro_filter": _macro_filter_mode,
                                        "verdict": _verdict.verdict,
                                        "reason": _verdict.reason,
                                        "tags": list(_verdict.regime_tags),
                                        "side": str(snapshot.decision.side),
                                    },
                                    correlation_id=_correlation_id,
                                )
                        else:
                            print("macro_filter: no series loaded — pass-through")
                    except Exception as _exc:  # pragma: no cover — defensive
                        print(
                            f"macro_filter_error: {type(_exc).__name__}: {_exc} "
                            "— pass-through"
                        )

                # ----------------------------------------------------------
                # Probability gate (slice-based veto). Default off.
                # GOLD_PROBABILITY_GATE: off | soft | hard.
                # ----------------------------------------------------------
                _prob_mode = os.environ.get("GOLD_PROBABILITY_GATE", "off").lower()
                _prob_block = False
                if (
                    _prob_mode in ("soft", "hard")
                    and snapshot.decision.status == "accept"
                    and snapshot.decision.side is not None
                    and snapshot.decision.family
                ):
                    try:
                        from .probability_gate import evaluate_probability_gate
                        _side_str = (
                            snapshot.decision.side.value
                            if hasattr(snapshot.decision.side, "value")
                            else str(snapshot.decision.side)
                        )
                        _verdict = evaluate_probability_gate(
                            family=snapshot.decision.family,
                            side=_side_str,
                            bars=next(iter(datasets.values())),
                        )
                        print(
                            f"probability_gate[{_prob_mode}]: "
                            f"verdict={_verdict.verdict} "
                            f"family={_verdict.family} "
                            f"reason={_verdict.reason}"
                        )
                        if _prob_mode == "hard" and _verdict.verdict == "block":
                            _prob_block = True
                    except Exception as _exc:  # pragma: no cover
                        print(
                            f"probability_gate_error: {type(_exc).__name__}: {_exc}"
                            " — pass-through"
                        )

                if (
                    snapshot.decision.status == "accept"
                    and not _macro_filter_block
                    and not _prob_block
                ):
                    new_pos = open_position_from_decision(
                        snapshot.decision,
                        next(iter(datasets.values())),
                    )
                    if new_pos is not None:
                        state = PaperState(
                            open_position=new_pos,
                            closed_positions=state.closed_positions,
                            paper_equity=state.paper_equity,
                            daily_peak_equity=state.daily_peak_equity,
                            last_updated=state.last_updated,
                            total_trades=state.total_trades,
                            winning_trades=state.winning_trades,
                            daily_reset_date=state.daily_reset_date,
                            daily_trades_opened=state.daily_trades_opened + 1,
                        )
                        print(
                            f"paper_trade_opened: side={new_pos.side} "
                            f"entry={new_pos.entry:.2f} stop={new_pos.stop:.2f} "
                            f"target={new_pos.target:.2f} family={new_pos.family} "
                            f"tf={new_pos.timeframe_minutes}m"
                        )

                        # ----------------------------------------------------
                        # LIVE: mirror the same decision into a real broker
                        # order (sized off live equity, not paper).
                        # Uses entry_price → pending stop when far from market.
                        # ----------------------------------------------------
                        if live_broker is not None:
                            try:
                                live_pos_now = live_broker.get_open_position()
                                live_pending_now = live_broker.get_pending_order()
                            except _BrokerError as exc:
                                print(f"live_order_skipped: {exc}")
                                live_pos_now = "error"
                                live_pending_now = None
                            if _equity_guard_tripped:
                                print("live_order_skipped: equity_guard_tripped")
                                live_pos_now = "error"  # blocks the place_market path
                            already_busy = (
                                live_pos_now is not None
                                or live_pending_now is not None
                            )
                            if (
                                not already_busy
                                and live_pos_now != "error"
                                and live_state.daily_trades_opened
                                < args.max_daily_trades
                            ):
                                try:
                                    live_acct = live_broker.get_account_info()
                                    risk_dollars = (
                                        live_acct.equity * args.risk_per_trade
                                    )
                                    side_enum = (
                                        _OrderSide.BUY
                                        if str(new_pos.side).lower().endswith("long")
                                        or str(new_pos.side).lower() == "buy"
                                        else _OrderSide.SELL
                                    )
                                    req = _OrderRequest(
                                        symbol=os.environ.get(
                                            "GOLD_SYMBOL", "GOLD"
                                        ),
                                        side=side_enum,
                                        risk_dollars=risk_dollars,
                                        stop_price=float(new_pos.stop),
                                        target_price=float(new_pos.target),
                                        entry_price=float(new_pos.entry),
                                        comment=(
                                            f"{new_pos.family}/"
                                            f"{new_pos.timeframe_minutes}m"
                                        )[:31],
                                    )
                                    res = live_broker.place_market_order(req)
                                    if res.accepted:
                                        live_state.daily_trades_opened += 1
                                        live_state.last_known_position_id = (
                                            res.broker_order_id
                                        )
                                        print(
                                            f"live_order_placed: "
                                            f"ticket={res.broker_order_id} "
                                            f"side={side_enum.value} "
                                            f"units={res.units:.2f} "
                                            f"entry={res.fill_price:.2f} "
                                            f"risk=${risk_dollars:.2f}"
                                        )
                                        if _infra_bus is not None:
                                            _infra_bus.publish(
                                                EventKind.ORDER_PLACED,
                                                {
                                                    "ticket": res.broker_order_id,
                                                    "side": side_enum.value,
                                                    "units": float(res.units or 0),
                                                    "entry_price": float(res.fill_price or 0),
                                                    "stop_price": float(new_pos.stop),
                                                    "target_price": float(new_pos.target),
                                                    "risk_dollars": float(risk_dollars),
                                                    "family": new_pos.family,
                                                    "timeframe_minutes": new_pos.timeframe_minutes,
                                                    "broker": live_broker.name,
                                                },
                                                correlation_id=_correlation_id,
                                            )
                                            try:
                                                _infra_db.upsert_pending_order({
                                                    "ticket": res.broker_order_id,
                                                    "symbol": req.symbol,
                                                    "side": side_enum.value,
                                                    "units": float(res.units or 0),
                                                    "entry_price": float(res.fill_price or 0),
                                                    "stop_price": float(new_pos.stop),
                                                    "target_price": float(new_pos.target),
                                                    "placed_at": datetime.now(timezone.utc).isoformat(),
                                                    "cancelled_at": None,
                                                    "magic": int(os.environ.get("GOLD_MAGIC", "20260507")),
                                                    "status": "resting",
                                                })
                                            except Exception:  # noqa: BLE001
                                                pass
                                    else:
                                        print(
                                            f"live_trade_rejected: {res.error}"
                                        )
                                        if _infra_bus is not None:
                                            _infra_bus.publish(
                                                EventKind.ORDER_REJECTED,
                                                {
                                                    "error": res.error,
                                                    "side": side_enum.value,
                                                    "entry_price": float(new_pos.entry),
                                                },
                                                correlation_id=_correlation_id,
                                            )
                                except _BrokerError as exc:
                                    print(f"live_trade_error: {exc}")
                                except Exception as exc:
                                    print(
                                        f"live_trade_unexpected_error: "
                                        f"{type(exc).__name__}: {exc}"
                                    )
                            elif already_busy:
                                what = (
                                    "position" if live_pos_now is not None
                                    else "pending order"
                                )
                                print(f"live_trade_skipped: {what} already at broker")
                            elif live_pos_now != "error":
                                print(
                                    f"live_trade_skipped: daily limit "
                                    f"({args.max_daily_trades}) reached"
                                )
            elif at_daily_limit:
                print(f"paper_scan_skipped: daily trade limit reached ({args.max_daily_trades})")
            else:
                print("paper_scan_skipped: position already open, monitoring only")

            save_paper_state(state, paper_state_path)

            if live_broker is not None:
                save_live_state(live_state, live_state_path)

            if iteration + 1 < args.iterations:
                time.sleep(args.interval_seconds)
        if _infra_bus is not None:
            _infra_bus.publish(
                EventKind.AGENT_CYCLE_FINISHED,
                {
                    "iterations": int(args.iterations),
                    "paper_equity": float(state.paper_equity),
                    "kill_switch": bool(state.kill_switch_triggered),
                },
                correlation_id=_correlation_id,
            )
        return

    if args.command == "holdout-eval":
        bars = load_bars_from_csv(args.csv_path)
        family = args.family
        # Apply --quick preset early so it overrides defaults but not explicit flags.
        if args.quick:
            if args.grid_sample == 0:
                args.grid_sample = 64
            if args.n_permutations == 5_000:
                args.n_permutations = 500
            args.skip_walk_forward = True
        if family == "liquidity_sweep":
            from .research.experiments import default_liquidity_grid
            from .research.sweep import LiquiditySweepParameters
            grid = default_liquidity_grid()
            def _lsf(params: LiquiditySweepParameters):
                return LiquiditySweepStrategy(
                    lookback=params.lookback,
                    atr_period=params.atr_period,
                    min_sweep_atr=params.min_sweep_atr,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_news_distance_minutes=params.min_news_distance_minutes,
                )
            factory = _lsf
        elif family == "compression_breakout":
            from .research.experiments import default_compression_grid, CompressionBreakoutParameters
            grid = default_compression_grid()
            def _cbf(params: CompressionBreakoutParameters):
                return CompressionBreakoutStrategy(
                    breakout_lookback=params.breakout_lookback,
                    compression_lookback=params.compression_lookback,
                    atr_period=params.atr_period,
                    max_compression_atr_ratio=params.max_compression_atr_ratio,
                    min_breakout_atr=params.min_breakout_atr,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_news_distance_minutes=params.min_news_distance_minutes,
                )
            factory = _cbf
        elif family == "london_breakout":
            from .research.experiments import default_london_breakout_grid, LondonBreakoutParameters
            grid = default_london_breakout_grid()
            def _lbf(params: LondonBreakoutParameters):
                return LondonBreakoutStrategy(
                    opening_range_bars=params.opening_range_bars,
                    atr_period=params.atr_period,
                    min_breakout_atr=params.min_breakout_atr,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                )
            factory = _lbf
        elif family == "trend_pullback":
            from .research.experiments import default_trend_pullback_grid, TrendPullbackParameters
            grid = default_trend_pullback_grid()
            def _tpf(params: TrendPullbackParameters):
                return TrendPullbackStrategy(
                    ema_fast=params.ema_fast,
                    ema_slow=params.ema_slow,
                    atr_period=params.atr_period,
                    trend_strength_min=params.trend_strength_min,
                    pullback_tolerance=params.pullback_tolerance,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                )
            factory = _tpf
        elif family == "ny_session_breakout":
            from .research.experiments import default_ny_session_breakout_grid, NYSessionBreakoutParameters
            grid = default_ny_session_breakout_grid()
            def _nysf(params: NYSessionBreakoutParameters):
                return NYSessionBreakoutStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_breakout_atr=params.min_breakout_atr,
                    min_range_atr=params.min_range_atr,
                    min_london_bars=params.min_london_bars,
                )
            factory = _nysf
        elif family == "momentum_burst":
            from .research.experiments import default_momentum_burst_grid, MomentumBurstParameters
            grid = default_momentum_burst_grid()
            def _mbf(params: MomentumBurstParameters):
                return MomentumBurstStrategy(
                    atr_period=params.atr_period,
                    min_body_atr=params.min_body_atr,
                    body_fraction=params.body_fraction,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                )
            factory = _mbf
        elif family == "asian_range_breakout":
            from .research.experiments import default_asian_range_grid, AsianRangeBreakoutParameters
            grid = default_asian_range_grid()
            def _arf(params: AsianRangeBreakoutParameters):
                return AsianRangeBreakoutStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_breakout_atr=params.min_breakout_atr,
                    min_range_atr=params.min_range_atr,
                    min_asian_bars=params.min_asian_bars,
                    min_atr_threshold=getattr(params, "min_atr_threshold", 0.0),
                )
            factory = _arf
        elif family == "previous_day_breakout":
            from .research.experiments import default_previous_day_breakout_grid, PreviousDayBreakoutParameters
            from .strategies.previous_day_breakout import PreviousDayBreakoutStrategy
            grid = default_previous_day_breakout_grid()
            def _pdbf(params: PreviousDayBreakoutParameters):
                return PreviousDayBreakoutStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_breakout_atr=params.min_breakout_atr,
                    stop_atr_buffer=params.stop_atr_buffer,
                )
            factory = _pdbf
        elif family == "opening_range_breakout":
            from .research.experiments import default_opening_range_breakout_grid, OpeningRangeBreakoutParameters
            from .strategies.opening_range_breakout import OpeningRangeBreakoutStrategy
            grid = default_opening_range_breakout_grid()
            def _orbf(params: OpeningRangeBreakoutParameters):
                return OpeningRangeBreakoutStrategy(
                    opening_range_bars=params.opening_range_bars,
                    atr_period=params.atr_period,
                    min_breakout_atr=params.min_breakout_atr,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                )
            factory = _orbf
        elif family == "asian_range_fade":
            from .research.experiments import default_asian_range_fade_grid, AsianRangeFadeParameters
            from .strategies.asian_range_fade import AsianRangeFadeStrategy
            grid = default_asian_range_fade_grid()
            def _arff(params: AsianRangeFadeParameters):
                return AsianRangeFadeStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_rejection_atr=params.min_rejection_atr,
                    min_range_atr=params.min_range_atr,
                    stop_atr_buffer=params.stop_atr_buffer,
                )
            factory = _arff
        elif family == "fair_value_gap":
            from .research.experiments import default_fair_value_gap_grid, FairValueGapParameters
            from .strategies.fair_value_gap import FairValueGapStrategy
            grid = default_fair_value_gap_grid()
            def _fvgf(params: FairValueGapParameters):
                return FairValueGapStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_gap_atr=params.min_gap_atr,
                    fvg_lookback=params.fvg_lookback,
                    stop_buffer_atr=params.stop_buffer_atr,
                )
            factory = _fvgf
        elif family == "inversion_fair_value_gap":
            from .research.experiments import default_inversion_fair_value_gap_grid, InversionFairValueGapParameters
            from .strategies.inversion_fair_value_gap import InversionFairValueGapStrategy
            grid = default_inversion_fair_value_gap_grid()
            def _ifvgf(params: InversionFairValueGapParameters):
                return InversionFairValueGapStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_gap_atr=params.min_gap_atr,
                    fvg_lookback=params.fvg_lookback,
                    inversion_lookback=params.inversion_lookback,
                    retest_lookback=params.retest_lookback,
                    stop_buffer_atr=params.stop_buffer_atr,
                )
            factory = _ifvgf
        elif family == "rsi_divergence":
            from .research.experiments import default_rsi_divergence_grid, RsiDivergenceParameters
            from .strategies.rsi_divergence import RsiDivergenceStrategy
            grid = default_rsi_divergence_grid()
            def _rsidf(params: RsiDivergenceParameters):
                return RsiDivergenceStrategy(
                    rsi_period=params.rsi_period,
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    overbought=params.overbought,
                    oversold=params.oversold,
                    pivot_window=params.pivot_window,
                    pivot_lookback=params.pivot_lookback,
                    min_pivot_separation=params.min_pivot_separation,
                    stop_buffer_atr=params.stop_buffer_atr,
                )
            factory = _rsidf
        elif family == "ny_close_compression":
            from .research.experiments import default_ny_close_compression_grid, NYCloseCompressionParameters
            from .strategies.ny_close_compression import NYCloseCompressionStrategy
            grid = default_ny_close_compression_grid()
            def _nyccf(params: NYCloseCompressionParameters):
                return NYCloseCompressionStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_breakout_atr=params.min_breakout_atr,
                    min_range_atr=params.min_range_atr,
                    max_range_atr=params.max_range_atr,
                    min_range_bars=params.min_range_bars,
                )
            factory = _nyccf
        elif family == "session_continuation":
            from .research.experiments import default_session_continuation_grid, SessionContinuationParameters
            from .strategies.session_continuation import SessionContinuationStrategy
            grid = default_session_continuation_grid()
            def _scf(params: SessionContinuationParameters):
                return SessionContinuationStrategy(
                    atr_period=params.atr_period,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_session_quantile=params.min_session_quantile,
                    min_range_atr=params.min_range_atr,
                    entry_slippage_buffer=params.entry_slippage_buffer,
                )
            factory = _scf
        elif family == "dxy_lead_lag":
            from .research.experiments import default_dxy_lead_lag_grid, DXYLeadLagParameters
            from .strategies.dxy_lead_lag import DXYLeadLagStrategy
            grid = default_dxy_lead_lag_grid()
            def _dxyllf(params: DXYLeadLagParameters):
                return DXYLeadLagStrategy(
                    lookback=params.lookback,
                    min_dxy_drop=params.min_dxy_drop,
                    max_gold_response=params.max_gold_response,
                    atr_period=params.atr_period,
                    stop_atr_mult=params.stop_atr_mult,
                    risk_reward=params.risk_reward,
                    max_spread=params.max_spread,
                    min_atr_threshold=params.min_atr_threshold,
                )
            factory = _dxyllf
        elif family == "real_yield_reversal":
            from .research.experiments import default_real_yield_reversal_grid, RealYieldReversalParameters
            from .strategies.real_yield_reversal import RealYieldReversalStrategy
            from .data import load_macro_frame
            macro_frame = load_macro_frame(args.macro_cache_dir)
            if "real10y" not in macro_frame:
                raise SystemExit(
                    f"real10y series not found in {args.macro_cache_dir}.  "
                    "Run 'gold-trader sync-macro' first."
                )
            grid = default_real_yield_reversal_grid()
            def _ryrf(params: RealYieldReversalParameters):
                return RealYieldReversalStrategy(
                    macro=macro_frame,
                    yield_lookback_days=params.yield_lookback_days,
                    min_yield_move_bps=params.min_yield_move_bps,
                    atr_period=params.atr_period,
                    stop_atr_mult=params.stop_atr_mult,
                    risk_reward=params.risk_reward,
                    enter_longs=params.enter_longs,
                    enter_shorts=params.enter_shorts,
                    max_spread=params.max_spread,
                    min_atr_threshold=params.min_atr_threshold,
                )
            factory = _ryrf
        elif family == "timed_horizon_macro_regime":
            from .research.experiments import (
                default_timed_horizon_macro_regime_grid,
                TimedHorizonMacroRegimeParameters,
            )
            from .strategies.timed_horizon_macro_regime import (
                TimedHorizonMacroRegimeStrategy,
            )
            from .data import load_macro_frame
            macro_frame = load_macro_frame(args.macro_cache_dir)
            for _req in ("real10y", "dxy", "vix"):
                if _req not in macro_frame:
                    raise SystemExit(
                        f"macro series {_req!r} not found in {args.macro_cache_dir}.  "
                        "Run 'gold-trader sync-macro' first."
                    )
            grid = default_timed_horizon_macro_regime_grid()
            def _thmrf(params: TimedHorizonMacroRegimeParameters):
                return TimedHorizonMacroRegimeStrategy(
                    macro=macro_frame,
                    real_yield_lookback_days=params.real_yield_lookback_days,
                    real_yield_max_change_bps=params.real_yield_max_change_bps,
                    vix_lookback_days=params.vix_lookback_days,
                    vix_max_change_abs=params.vix_max_change_abs,
                    require_dxy_flat=params.require_dxy_flat,
                    dxy_lookback_days=params.dxy_lookback_days,
                    dxy_max_abs_change_pct=params.dxy_max_abs_change_pct,
                    atr_period=params.atr_period,
                    far_atr_mult=params.far_atr_mult,
                    once_per_day=params.once_per_day,
                    require_bullish_close=params.require_bullish_close,
                    max_spread=params.max_spread,
                )
            factory = _thmrf

        # Apply grid sampling AFTER the family-specific grid is built.
        if args.grid_sample and args.grid_sample > 0 and len(grid) > args.grid_sample:
            import random as _r
            rng = _r.Random(args.grid_sample_seed)
            grid = rng.sample(list(grid), args.grid_sample)
        # Workload preview: tells the user up-front what they signed up for.
        n_train = int(len(bars) * (1.0 - args.holdout_fraction))
        wf_windows = 0 if args.skip_walk_forward else max(
            1, (n_train - int(n_train * 0.60) - int(n_train * 0.20)) // max(1, int(n_train * 0.20)) + 1
        )
        backtests_in_train = len(grid) * (1 + wf_windows)
        print(
            f"[plan] family={family} bars={len(bars)} train={n_train} "
            f"grid={len(grid)} wf_windows={wf_windows} workers={args.workers} "
            f"approx_train_backtests={backtests_in_train} permutations={args.n_permutations}"
        )

        result = run_holdout_evaluation(
            bars=bars,
            param_grid=grid,
            strategy_factory=factory,
            config=BacktestConfig(commission_per_trade=10.0),
            holdout_fraction=args.holdout_fraction,
            min_train_trades=args.min_train_trades,
            n_permutations=args.n_permutations,
            family=family,
            family_name=family if family not in ("real_yield_reversal", "timed_horizon_macro_regime") else "",
            n_workers=1 if family in ("real_yield_reversal", "timed_horizon_macro_regime") else args.workers,
            skip_walk_forward=args.skip_walk_forward,
        )
        print(f"family: {result.family}")
        print(f"total_bars: {len(bars)}")
        print(f"train_bars: {result.train_bars}")
        print(f"holdout_bars: {result.holdout_bars}")
        print(f"best_params: {result.best_params}")
        print(f"train_pf: {result.train_pf:.4f}")
        print(f"")
        print(f"--- held-out out-of-sample ---")
        print(_format_summary(result.holdout_summary, "holdout backtest"))
        print(f"holdout_permutation_p: {result.holdout_permutation.p_value:.4f}")
        print(f"holdout_permutation_verdict: {result.holdout_permutation.verdict}")
        print(f"")
        print(f"--- true walk-forward (train portion) ---")
        print(f"wf_windows: {result.true_walk_forward.window_count}")
        print(f"wf_positive_ratio: {result.true_walk_forward.positive_window_ratio:.0%}")
        print(f"wf_avg_r: {result.true_walk_forward.average_r:.4f}")
        print(f"wf_avg_pf: {result.true_walk_forward.average_profit_factor:.4f}")
        print(f"wf_total_test_trades: {result.true_walk_forward.total_test_trades}")
        print(f"")
        print(f"verdict: {result.verdict}")
        return

    if args.command == "mine-patterns":
        import csv as _csv
        from .data import load_bars_from_csv as _load_bars
        from .research.features import build_feature_matrix
        from .research.pattern_miner import (
            MinerConfig, mine_patterns_parallel,
        )
        bars = _load_bars(args.csv_path)
        if not bars:
            raise SystemExit(f"no bars loaded from {args.csv_path}")
        print(
            f"loaded {len(bars)} bars "
            f"({bars[0].timestamp} → {bars[-1].timestamp})"
        )
        print("building feature matrix ...")
        fm = build_feature_matrix(bars)
        print(f"  {len(fm.names())} features")
        cfg = MinerConfig(
            horizon_bars=args.horizon,
            train_fraction=args.train_fraction,
            max_combo_size=args.max_combo_size,
            min_signals=args.min_signals,
            min_effect_r=args.min_effect_r,
            fdr_q=args.fdr_q,
            bootstrap_blocks=args.bootstrap_blocks,
            block_size=args.block_size,
        )
        n_combos = len(fm.names())
        if args.max_combo_size >= 2:
            n_combos += len(fm.names()) * (len(fm.names()) - 1) // 2
        if args.max_combo_size >= 3:
            n_features = len(fm.names())
            n_combos += n_features * (n_features - 1) * (n_features - 2) // 6
        print(
            f"mining ≤{args.max_combo_size}-feature conjunctions "
            f"(~{n_combos} candidates) "
            f"horizon={cfg.horizon_bars} bars; "
            f"FDR q={cfg.fdr_q:.2f} ..."
        )
        survivors = mine_patterns_parallel(bars, fm, config=cfg, progress=True)
        print(f"FDR-controlled survivors with holdout data: {len(survivors)}")
        if not survivors:
            print("(no patterns cleared the FDR + holdout threshold)")
            return
        print()
        print(
            f"{'#':>3} {'dir':<5} {'features':<48} "
            f"{'n_tr':>5} {'mean_R_tr':>9} {'p_adj':>8} "
            f"{'n_ho':>5} {'mean_R_ho':>9} {'p_ho':>7} "
            f"{'sta':>4} {'shp':>6}"
        )
        print("-" * 124)
        top = survivors[: args.top]
        for i, p in enumerate(top, 1):
            feats = " & ".join(p.features)
            if len(feats) > 47:
                feats = feats[:44] + "..."
            print(
                f"{i:>3} {p.direction:<5} {feats:<48} "
                f"{p.n_train:>5} {p.train_mean_r:>+9.3f} "
                f"{p.train_p_adj:>8.4f} "
                f"{p.n_holdout:>5} {p.holdout_mean_r:>+9.3f} "
                f"{p.holdout_p:>7.3f} "
                f"{p.holdout_thirds_consistent:>4d} "
                f"{p.holdout_sharpe:>+6.3f}"
            )
        if args.output:
            outp = Path(args.output)
            outp.parent.mkdir(parents=True, exist_ok=True)
            with outp.open("w", newline="") as fh:
                w = _csv.writer(fh)
                w.writerow([
                    "rank", "direction", "features",
                    "n_train", "train_mean_r", "train_win_rate",
                    "train_t_stat", "train_p", "train_p_adj",
                    "n_holdout", "holdout_mean_r", "holdout_win_rate",
                    "holdout_t_stat", "holdout_p",
                    "holdout_thirds_consistent", "holdout_sharpe",
                ])
                for i, p in enumerate(survivors, 1):
                    w.writerow([
                        i, p.direction, " & ".join(p.features),
                        p.n_train, f"{p.train_mean_r:.6f}",
                        f"{p.train_win_rate:.4f}",
                        f"{p.train_t_stat:.4f}",
                        f"{p.train_p:.6f}", f"{p.train_p_adj:.6f}",
                        p.n_holdout, f"{p.holdout_mean_r:.6f}",
                        f"{p.holdout_win_rate:.4f}",
                        f"{p.holdout_t_stat:.4f}",
                        f"{p.holdout_p:.6f}",
                        p.holdout_thirds_consistent,
                        f"{p.holdout_sharpe:.6f}",
                    ])
            print(f"\nwrote {len(survivors)} survivors → {outp}")
        return

    if args.command == "mine-all":
        import csv as _csv
        from collections import defaultdict
        from .data import load_bars_from_csv as _load_bars
        from .data.macro import load_macro_frame
        from .research.features import build_feature_matrix
        from .research.macro_features import add_macro_features
        from .research.pattern_miner import (
            MinerConfig, mine_patterns_parallel,
        )
        base_bars = _load_bars(args.csv_path)
        if not base_bars:
            raise SystemExit(f"no bars loaded from {args.csv_path}")
        macro = None
        if args.with_macro:
            macro = load_macro_frame(args.macro_cache_dir)
            if not macro.series:
                print(
                    f"WARNING: --with-macro set but no series found in "
                    f"{args.macro_cache_dir}; continuing without macro."
                )
                macro = None
            else:
                print(
                    f"loaded macro frame: {sorted(macro.names())}"
                )
        delta = (base_bars[1].timestamp - base_bars[0].timestamp).total_seconds() / 60
        native_tf = int(round(delta))
        print(
            f"loaded {len(base_bars)} bars at {native_tf}m "
            f"({base_bars[0].timestamp} → {base_bars[-1].timestamp})"
        )
        timeframes = [int(t) for t in args.timeframes.split(",") if t]
        horizons = [int(h) for h in args.horizons.split(",") if h]
        outdir = Path(args.output_dir)
        outdir.mkdir(parents=True, exist_ok=True)

        all_rows: list[dict] = []
        for tf in timeframes:
            if tf == native_tf:
                bars = base_bars
            elif tf > native_tf and tf % native_tf == 0:
                bars = resample_bars(base_bars, tf)
            else:
                print(f"  skip tf={tf}m (not a multiple of native {native_tf}m)")
                continue
            if len(bars) < 500:
                print(f"  skip tf={tf}m (only {len(bars)} bars)")
                continue
            print(f"\n=== timeframe {tf}m: {len(bars)} bars ===")
            print("  building feature matrix ...")
            fm = build_feature_matrix(bars)
            if macro is not None:
                fm = add_macro_features(fm, bars, macro)
            print(f"  {len(fm.names())} features")
            for h in horizons:
                min_sig = max(20, min(args.min_signals, len(bars) // 100))
                cfg = MinerConfig(
                    horizon_bars=h,
                    train_fraction=args.train_fraction,
                    max_combo_size=args.max_combo_size,
                    min_signals=min_sig,
                    min_effect_r=args.min_effect_r,
                    fdr_q=args.fdr_q,
                    bootstrap_blocks=args.bootstrap_blocks,
                    block_size=max(4, min(args.block_size, len(bars) // 200)),
                )
                print(
                    f"  --- horizon {h} bars, min_signals={min_sig}, "
                    f"max_combo={cfg.max_combo_size}"
                )
                survivors = mine_patterns_parallel(
                    bars, fm, config=cfg, progress=False,
                )
                print(f"      {len(survivors)} FDR+holdout survivors")
                for p in survivors:
                    all_rows.append({
                        "timeframe": tf,
                        "horizon": h,
                        "direction": p.direction,
                        "features": " & ".join(p.features),
                        "n_train": p.n_train,
                        "train_mean_r": p.train_mean_r,
                        "train_p_adj": p.train_p_adj,
                        "n_holdout": p.n_holdout,
                        "holdout_mean_r": p.holdout_mean_r,
                        "holdout_win_rate": p.holdout_win_rate,
                        "holdout_p": p.holdout_p,
                        "holdout_thirds_consistent": (
                            p.holdout_thirds_consistent
                        ),
                        "holdout_sharpe": p.holdout_sharpe,
                    })

        master_path = outdir / "all_survivors.csv"
        with master_path.open("w", newline="") as fh:
            w = _csv.writer(fh)
            cols = [
                "timeframe", "horizon", "direction", "features",
                "n_train", "train_mean_r", "train_p_adj",
                "n_holdout", "holdout_mean_r", "holdout_win_rate",
                "holdout_p", "holdout_thirds_consistent", "holdout_sharpe",
            ]
            w.writerow(cols)
            for r in all_rows:
                w.writerow([r[c] for c in cols])
        print(f"\nwrote {len(all_rows)} (tf,horizon,combo) rows → {master_path}")

        combo_hits: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for r in all_rows:
            key = (r["features"], r["direction"])
            combo_hits[key].append(r)
        replicators = [
            (k, v) for k, v in combo_hits.items()
            if len({rr["timeframe"] for rr in v}) >= 2
        ]
        replicators.sort(
            key=lambda kv: -sum(
                abs(rr["holdout_mean_r"]) * (1 + rr["holdout_thirds_consistent"])
                for rr in kv[1]
            ),
        )
        repl_path = outdir / "cross_tf_replicators.csv"
        with repl_path.open("w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow([
                "features", "direction", "n_timeframes", "n_hits",
                "mean_holdout_r", "min_holdout_p",
                "avg_thirds_consistent", "hits_detail",
            ])
            for (feats, direction), hits in replicators:
                tfs = sorted({h["timeframe"] for h in hits})
                detail = "; ".join(
                    f"{h['timeframe']}m/h{h['horizon']}: "
                    f"R={h['holdout_mean_r']:+.3f} p={h['holdout_p']:.3f} "
                    f"st={h['holdout_thirds_consistent']}"
                    for h in hits
                )
                w.writerow([
                    feats, direction, len(tfs), len(hits),
                    f"{sum(h['holdout_mean_r'] for h in hits)/len(hits):.4f}",
                    f"{min(h['holdout_p'] for h in hits):.4f}",
                    f"{sum(h['holdout_thirds_consistent'] for h in hits)/len(hits):.2f}",
                    detail,
                ])
        print(
            f"wrote {len(replicators)} cross-timeframe replicators "
            f"→ {repl_path}"
        )
        if replicators:
            print("\n--- Top 25 cross-timeframe replicators ---")
            for (feats, direction), hits in replicators[:25]:
                tfs = sorted({h["timeframe"] for h in hits})
                avg_st = sum(
                    h["holdout_thirds_consistent"] for h in hits
                ) / len(hits)
                avg_r = sum(h["holdout_mean_r"] for h in hits) / len(hits)
                min_p = min(h["holdout_p"] for h in hits)
                f_short = feats if len(feats) <= 50 else feats[:47] + "..."
                print(
                    f"  {direction:<5} {f_short:<50} "
                    f"tfs={tfs} avg_R={avg_r:+.3f} "
                    f"min_p={min_p:.3f} avg_st={avg_st:.2f}"
                )
        return

    if args.command == "calibrate-scores":
        bars = load_bars_from_csv(args.csv_path)
        family = args.family
        if family == "liquidity_sweep":
            strategy = LiquiditySweepStrategy()
        elif family == "asian_range_breakout":
            strategy = AsianRangeBreakoutStrategy()
        elif family == "london_breakout":
            strategy = LondonBreakoutStrategy()
        elif family == "trend_pullback":
            strategy = TrendPullbackStrategy()
        elif family == "ny_session_breakout":
            strategy = NYSessionBreakoutStrategy()
        elif family == "momentum_burst":
            strategy = MomentumBurstStrategy()
        else:
            strategy = CompressionBreakoutStrategy()
        cal = calibrate_score_system(bars, strategy, BacktestConfig())
        print(f"family: {family}")
        print(f"total_trades: {cal.total_trades}")
        print(f"")
        if cal.brackets:
            print(f"{'bracket':<10} {'trades':>7} {'wins':>5} {'wr':>7} {'avg_r':>8} {'pf':>8}")
            print("-" * 55)
            for b in cal.brackets:
                print(
                    f"{b.label:<10} {b.trades:>7} {b.wins:>5} "
                    f"{b.win_rate:>7.1%} {b.avg_r:>8.3f} {b.profit_factor:>8.2f}"
                )
            print()
        print(f"verdict: {cal.verdict}")
        return

    if args.command == "permutation-test":
        bars = load_bars_from_csv(args.csv_path)
        if args.family == "liquidity_sweep":
            strategy = LiquiditySweepStrategy()
        elif args.family == "asian_range_breakout":
            strategy = AsianRangeBreakoutStrategy()
        elif args.family == "london_breakout":
            strategy = LondonBreakoutStrategy()
        elif args.family == "trend_pullback":
            strategy = TrendPullbackStrategy()
        elif args.family == "ny_session_breakout":
            strategy = NYSessionBreakoutStrategy()
        elif args.family == "momentum_burst":
            strategy = MomentumBurstStrategy()
        elif args.family == "previous_day_breakout":
            from .strategies.previous_day_breakout import PreviousDayBreakoutStrategy
            strategy = PreviousDayBreakoutStrategy()
        elif args.family == "opening_range_breakout":
            from .strategies.opening_range_breakout import OpeningRangeBreakoutStrategy
            strategy = OpeningRangeBreakoutStrategy()
        elif args.family == "asian_range_fade":
            from .strategies.asian_range_fade import AsianRangeFadeStrategy
            strategy = AsianRangeFadeStrategy()
        elif args.family == "fair_value_gap":
            from .strategies.fair_value_gap import FairValueGapStrategy
            strategy = FairValueGapStrategy()
        elif args.family == "ny_close_compression":
            from .strategies.ny_close_compression import NYCloseCompressionStrategy
            strategy = NYCloseCompressionStrategy()
        else:
            strategy = CompressionBreakoutStrategy()
        config = BacktestConfig(kill_switch_drawdown_fraction=None, commission_per_trade=10.0)
        result = run_permutation_test(
            bars=bars,
            strategy=strategy,
            config=config,
            n_permutations=args.n_permutations,
            seed=args.seed,
        )
        print(f"strategy: {strategy.name}")
        print(f"csv: {args.csv_path}")
        print(f"n_trades: {result.n_trades}")
        print(f"observed_pf: {result.observed_pf:.4f}")
        print(f"observed_avg_r: {result.observed_avg_r:.4f}")
        print(f"n_permutations: {result.n_permutations}")
        print(f"p_value: {result.p_value:.4f}")
        print(f"percentile_rank: {result.percentile_rank:.1f}")
        print(f"null_mean_pf: {result.null_mean_pf:.4f}")
        print(f"null_median_pf: {result.null_median_pf:.4f}")
        print(f"verdict: {result.verdict}")
        return

    if args.command == "combine-csv":
        from glob import glob as _glob
        import itertools

        # Collect all input files
        input_paths: list[Path] = []
        for pattern in args.inputs:
            matched = sorted(_glob(pattern))
            if matched:
                input_paths.extend(Path(p) for p in matched)
            else:
                # Treat as literal path
                input_paths.append(Path(pattern))

        if not input_paths:
            print("No input files found.")
            return

        all_bars: list = []
        for csv_path in input_paths:
            bars = load_bars_from_csv(csv_path)
            all_bars.extend(bars)
            print(f"loaded: {csv_path.name} ({len(bars)} bars)")

        # Sort by timestamp and deduplicate
        all_bars.sort(key=lambda b: b.timestamp)
        seen: set = set()
        deduped: list = []
        for bar in all_bars:
            key = bar.timestamp.isoformat()
            if key not in seen:
                seen.add(key)
                deduped.append(bar)

        removed = len(all_bars) - len(deduped)
        output_path = Path(args.output)
        write_bars_to_csv(deduped, output_path)
        print(f"total_input_bars: {len(all_bars)}")
        print(f"duplicates_removed: {removed}")
        print(f"output_bars: {len(deduped)}")
        print(f"output_range: {deduped[0].timestamp.isoformat()} to {deduped[-1].timestamp.isoformat()}")
        print(f"output: {output_path}")
        return

    if args.command == "extend-csv":
        # Incremental extension of an existing research CSV.
        # Reads the last bar timestamp, then downloads only the gap to --end-date.
        target_path = Path(args.csv_path)
        last_ts = read_last_bar_timestamp(target_path)

        end_date = _parse_date(args.end_date) if args.end_date else _utc_today()

        if last_ts is not None:
            fetch_start = last_ts.date()
            print(f"existing_file: {target_path}")
            print(f"last_bar: {last_ts.isoformat()}")
            print(f"fetching: {fetch_start} to {end_date}")
        else:
            if not args.start_date:
                print("error: --start-date is required when the target CSV does not exist yet")
                return
            fetch_start = _parse_date(args.start_date)
            print(f"new_file: {target_path}")
            print(f"fetching: {fetch_start} to {end_date}")

        new_bars = download_dukascopy_bars(
            symbol=args.symbol,
            start_date=fetch_start,
            end_date=end_date,
            interval_minutes=args.interval_minutes,
            max_workers=args.max_workers,
            price_decimals=args.price_decimals,
        )
        added = append_bars_to_csv(new_bars, target_path, after=last_ts)
        print(f"fetched_bars: {len(new_bars)}")
        print(f"new_bars_appended: {added}")
        updated_ts = read_last_bar_timestamp(target_path)
        print(f"updated_last_bar: {updated_ts.isoformat() if updated_ts else 'n/a'}")
        return

    if args.command == "merge-dxy":
        from .data import merge_dxy_into_csv
        filled = merge_dxy_into_csv(
            xauusd_csv=args.xauusd_csv,
            eurusd_csv=args.eurusd_csv,
            output_csv=args.output if args.output else None,
        )
        dest = args.output if args.output else args.xauusd_csv
        total = sum(1 for _ in open(dest)) - 1  # row count minus header
        print(f"xauusd_csv: {args.xauusd_csv}")
        print(f"eurusd_csv: {args.eurusd_csv}")
        print(f"output: {dest}")
        print(f"xauusd_bars: {total}")
        print(f"bars_with_dxy: {filled}")
        print(f"coverage: {filled / total:.1%}" if total > 0 else "coverage: 0%")
        return

    if args.command == "sync-macro":
        from .data import sync_macro_bundle, MACRO_BUNDLE
        end_d = _parse_date(args.end_date) if args.end_date else date.today()
        start_d = _parse_date(args.start_date) if args.start_date else end_d - timedelta(days=args.lookback_days)
        names = args.names.split(",") if args.names else None
        if names:
            unknown = [n for n in names if n not in MACRO_BUNDLE]
            if unknown:
                raise SystemExit(f"Unknown macro names: {unknown}.  Known: {sorted(MACRO_BUNDLE)}")
        cache_dir = Path(args.cache_dir)
        print(f"cache_dir: {cache_dir}")
        print(f"window: {start_d} -> {end_d}")
        print(f"refresh: {args.refresh}")
        status = sync_macro_bundle(cache_dir, start=start_d, end=end_d, refresh=args.refresh, names=names)
        ok = sum(1 for v in status.values() if v == "ok")
        print(f"results: {ok}/{len(status)} ok")
        for name, msg in status.items():
            print(f"  {name:<10} {msg}")
        return

    if args.command == "show-macro":
        from .data import load_macro_frame
        frame = load_macro_frame(args.cache_dir)
        if not frame.series:
            print(f"No cached macro series in {args.cache_dir}.  Run sync-macro first.")
            return
        for name, series in sorted(frame.series.items()):
            n = len(series.points)
            if n == 0:
                print(f"{name:<10} EMPTY")
                continue
            first = series.points[0]
            last = series.points[-1]
            print(
                f"{name:<10} src={series.source:<5} n={n:<5} "
                f"first={first.timestamp.date()} ({first.value:.4f})  "
                f"last={last.timestamp.date()} ({last.value:.4f})"
            )
        return

    if args.command == "paper-report":
        paper_state_path = Path(args.state_path)
        state = load_paper_state(paper_state_path)
        closed = state.closed_positions
        print(f"paper_equity: {state.paper_equity:.2f}")
        print(f"total_trades: {state.total_trades}")
        print(f"win_rate: {state.win_rate:.1%}")
        if closed:
            winning_r = sum(p.pnl_r for p in closed if p.pnl_r is not None and p.pnl_r > 0)
            losing_r = abs(sum(p.pnl_r for p in closed if p.pnl_r is not None and p.pnl_r <= 0))
            pf = winning_r / losing_r if losing_r > 0 else float("inf")
            avg_r = sum(p.pnl_r for p in closed if p.pnl_r is not None) / len(closed)
            print(f"profit_factor: {pf:.4f}")
            print(f"avg_r: {avg_r:.4f}")
            print(f"")
            print(f"{'opened_at':<27} {'family':<22} {'side':<6} {'entry':>8} {'exit':>8} {'pnl_r':>7} {'reason'}")
            print("-" * 100)
            for p in closed[-50:]:  # show last 50 trades
                pnl_str = f"{p.pnl_r:+.3f}" if p.pnl_r is not None else "  n/a"
                exit_str = f"{p.closed_price:.2f}" if p.closed_price is not None else "    n/a"
                print(f"{p.opened_at:<27} {p.family:<22} {p.side:<6} {p.entry:>8.2f} {exit_str:>8} {pnl_str:>7} {p.exit_reason or ''}")
        if state.open_position:
            op = state.open_position
            print(f"")
            print(f"open_position: {op.family} {op.side} entry={op.entry:.2f} stop={op.stop:.2f} target={op.target:.2f} opened={op.opened_at}")
        return

    if args.command == "broker-info":
        from .live import get_broker_from_env, BrokerError as _BrokerError

        try:
            broker = get_broker_from_env()
        except _BrokerError as exc:
            print(f"broker_error: {exc}")
            raise SystemExit(2)
        print(f"broker: {broker.name}")
        try:
            info = broker.get_account_info()
        except _BrokerError as exc:
            print(f"account_info_error: {exc}")
            raise SystemExit(2)
        print(f"equity: {info.equity:.2f}")
        print(f"balance: {info.balance:.2f}")
        print(f"currency: {info.currency}")
        print(f"margin_used: {info.margin_used:.2f}")
        print(f"margin_free: {info.margin_free:.2f}")
        print(f"leverage: {info.leverage:.0f}")
        try:
            op = broker.get_open_position()
        except _BrokerError as exc:
            print(f"open_position_error: {exc}")
            return
        if op is None:
            print("open_position: none")
        else:
            print(
                f"open_position: id={op.broker_order_id} symbol={op.symbol} "
                f"side={op.side.value} units={op.units} entry={op.entry_price:.2f} "
                f"stop={op.stop_price:.2f} target={op.target_price:.2f} "
                f"upnl={op.unrealised_pnl:.2f}"
            )
        return

    if args.command == "panic":
        # Cancel every pending and close every open position for our magic.
        # No flags, no confirmation: this is the fire-extinguisher.
        from .live import get_broker_from_env, BrokerError as _BrokerError
        from .infra import (
            configure_logging, open_state_db, EventBus, EventKind,
            flatten_account,
        )

        configure_logging(log_dir=Path("logs"), level="INFO")
        try:
            broker = get_broker_from_env()
        except _BrokerError as exc:
            print(f"broker_error: {exc}")
            raise SystemExit(2)
        magic = int(os.environ.get("GOLD_MAGIC", "20260507"))
        db_path = Path(args.db_path) if args.db_path else (
            Path(args.output_dir) / "state.db" if args.output_dir
            else Path("data/state.db")
        )
        try:
            db = open_state_db(db_path)
            bus = EventBus(db, jsonl_path=Path("logs/events.jsonl"))
        except Exception as exc:  # noqa: BLE001
            print(f"infra_init_failed: {exc}")
            db = None
            bus = None

        print(f"panic: broker={broker.name} magic={magic}")
        report = flatten_account(broker, magic=magic, reason="panic_cli")
        print(f"cancelled_pending: {report.cancelled_pending}")
        print(f"closed_positions: {report.closed_positions}")
        if report.errors:
            print(f"errors: {report.errors}")
        if bus is not None:
            bus.publish(
                EventKind.KILL_SWITCH_TRIGGERED,
                {
                    "rule": "panic_cli",
                    "reason": "manual panic command",
                    "cancelled_pending": list(report.cancelled_pending),
                    "closed_positions": list(report.closed_positions),
                    "errors": list(report.errors),
                    "magic": magic,
                },
            )
        if report.errors:
            raise SystemExit(1)
        return

    if args.command == "dashboard":
        from .live import get_broker_from_env, BrokerError as _BrokerError
        from .reports import write_dashboard

        paper_state_path = Path(args.state_path)
        state = load_paper_state(paper_state_path, starting_equity=args.starting_equity)

        broker_name = "paper"
        account = None
        op_broker = None
        broker_error: str | None = None
        if not args.no_broker:
            try:
                broker = get_broker_from_env()
                broker_name = broker.name
                account = broker.get_account_info()
                op_broker = broker.get_open_position()
            except _BrokerError as exc:
                broker_error = str(exc)
            except Exception as exc:  # never let dashboard rendering fail
                broker_error = f"{type(exc).__name__}: {exc}"

        out = write_dashboard(
            state,
            args.output,
            broker_name=broker_name,
            account=account,
            op_broker=op_broker,
            broker_error=broker_error,
            starting_equity=args.starting_equity,
            title=args.title,
        )
        print(f"dashboard_written: {out}")
        return

    if args.command == "serve":
        from .web import serve as _serve
        _serve(host=args.host, port=args.port)
        return

    if args.command == "check-concurrence":
        from .backtest import concurrence_at_bar
        from .research.family_grids import (
            all_self_contained_families, family_spec,
        )
        bars = load_bars_from_csv(args.csv_path)
        if not bars:
            raise SystemExit(f"no bars loaded from {args.csv_path}")
        # Resolve target index
        if args.at:
            target = datetime.fromisoformat(args.at)
            # Find latest bar at or before target
            idx = None
            for i, b in enumerate(bars):
                if b.timestamp <= target:
                    idx = i
                else:
                    break
            if idx is None:
                raise SystemExit(f"no bar at or before {target}")
        else:
            idx = len(bars) - 1
        # Build default-param strategies for every self-contained family
        strategies = []
        for fam in all_self_contained_families():
            spec = family_spec(fam)
            strategies.append(spec.factory(spec.grid[0]))
        firing = concurrence_at_bar(bars, strategies, idx)
        long_strats = firing[Side.LONG]
        short_strats = firing[Side.SHORT]
        bar = bars[idx]
        print(f"bar         : {bar.timestamp.isoformat()}  close={bar.close:.3f}")
        print(f"index       : {idx} / {len(bars) - 1}")
        print(f"LONG  ({len(long_strats):>2}): {', '.join(long_strats) or '-'}")
        print(f"SHORT ({len(short_strats):>2}): {', '.join(short_strats) or '-'}")
        n_max = max(len(long_strats), len(short_strats))
        side = "LONG" if len(long_strats) >= len(short_strats) else "SHORT"
        if n_max >= args.gate_min:
            print(f"VERDICT     : ARMED ({side}, n={n_max} >= {args.gate_min})")
        else:
            print(f"VERDICT     : standby (n={n_max} < {args.gate_min})")
        return

    if args.command == "ensemble-backtest":
        import json
        from .backtest import run_ensemble_backtest, summarize_backtest
        from .research.family_grids import (
            all_self_contained_families, family_spec,
        )
        bars = load_bars_from_csv(args.csv_path)
        if not bars:
            raise SystemExit(f"no bars loaded from {args.csv_path}")
        config = BacktestConfig(
            slippage_bps=args.slippage_bps,
            commission_per_trade=args.commission,
            fill_aware_stops=args.fill_aware_stops,
        )
        weights: dict[str, float] = {}
        weights_path = Path(args.weights)
        if weights_path.exists():
            with weights_path.open() as f:
                weights = {
                    str(k): float(v)
                    for k, v in json.load(f).get("weights", {}).items()
                }
        strategies = []
        for fam in all_self_contained_families():
            spec = family_spec(fam)
            strategies.append(spec.factory(spec.grid[0]))
        result = run_ensemble_backtest(
            bars, strategies, config,
            gate_min=args.gate_min, weights=weights or None,
        )
        summary = summarize_backtest(result.backtest)
        print(f"strategies  : {len(strategies)}")
        print(f"bars        : {len(bars)}")
        print(f"signals_seen: {result.n_signals_total}")
        print(f"gated_in    : {result.n_signals_gated_in}  (>= {result.gate_min} concurrent)")
        print(f"trades      : {summary.total_trades}")
        print(f"win%        : {summary.win_rate:.1%}")
        print(f"PF          : {summary.profit_factor:.2f}")
        print(f"avgR        : {summary.average_r:+.3f}")
        print(f"final equity: {result.backtest.ending_equity:.2f}")
        if args.events_csv:
            import csv as _csv
            out = Path(args.events_csv)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w", newline="") as f:
                w = _csv.writer(f)
                w.writerow(
                    ["bar_index", "timestamp", "side", "n_strategies",
                     "strategies", "chosen_strategy", "score"]
                )
                for ev in result.events:
                    w.writerow([
                        ev.bar_index,
                        bars[ev.bar_index].timestamp.isoformat(),
                        ev.side.name,
                        len(ev.strategies),
                        "|".join(ev.strategies),
                        ev.chosen_strategy,
                        f"{ev.score:.2f}",
                    ])
            print(f"events_csv  : {out}")
        return

    if args.command == "slice-probabilities":
        from .research.family_grids import all_self_contained_families, family_spec
        from .research.probability_slicer import (
            compute_probability_table,
            compute_pooled_probability_table,
            write_probability_table,
        )
        bars = load_bars_from_csv(args.csv_path)
        if not bars:
            raise SystemExit(f"no bars loaded from {args.csv_path}")
        families = (
            all_self_contained_families()
            if args.family == "all"
            else [args.family]
        )
        config = BacktestConfig(
            slippage_bps=args.slippage_bps,
            commission_per_trade=args.commission,
            fill_aware_stops=args.fill_aware_stops,
        )
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for family in families:
            spec = family_spec(family)
            if args.pool_grid:
                strategies = [spec.factory(p) for p in spec.grid]
                table = compute_pooled_probability_table(
                    bars, strategies, config,
                    family=family,
                    include_pairs=not args.no_pairs,
                    min_pair_n=args.min_pair_n,
                )
            else:
                params = spec.grid[args.params_index]
                strategy = spec.factory(params)
                table = compute_probability_table(
                    bars,
                    strategy,
                    config,
                    family=family,
                    include_pairs=not args.no_pairs,
                    min_pair_n=args.min_pair_n,
                )
            path = out_dir / f"{family}.json"
            write_probability_table(table, path)
            edges = table.edge_slices(
                min_n=args.min_n,
                min_expectancy_r=args.min_expectancy_r,
                min_profit_factor=args.min_profit_factor,
            )
            print(
                f"{family}: trades={table.n_total} "
                f"base_pf={table.base_profit_factor:.2f} "
                f"base_avg_r={table.base_avg_r:+.3f} "
                f"edge_slices={len(edges)} -> {path}"
            )
            for s in edges[: args.show_top]:
                print(
                    f"    [{s.key}] n={s.n} pf={s.profit_factor:.2f} "
                    f"avg_r={s.avg_r:+.3f} expectancy={s.expectancy:+.3f} "
                    f"win_rate={s.win_rate:.0%} lci_r={s.lower_ci_r:+.3f}"
                )
        return

    if args.command == "dump-signals":
        # Pattern-recognition aid: dump every fired signal of a strategy on a
        # CSV, one row per closed trade, so the operator can eyeball whether
        # the rule fires on the same bars they would. No grid optimisation,
        # no permutation — purely "where does this rule trigger?".
        import csv as _csv
        from dataclasses import asdict as _asdict, fields as _dc_fields
        from .research.family_grids import family_spec
        bars = load_bars_from_csv(args.csv_path)
        if not bars:
            raise SystemExit(f"no bars loaded from {args.csv_path}")
        spec = family_spec(args.family)
        config = BacktestConfig(
            slippage_bps=args.slippage_bps,
            commission_per_trade=args.commission,
            fill_aware_stops=args.fill_aware_stops,
        )
        if args.pool_grid:
            grid_entries = list(enumerate(spec.grid))
        else:
            grid_entries = [(args.params_index, spec.grid[args.params_index])]

        # entry_time -> first occurrence wins when pooling (consistent with
        # slice-probabilities pooling semantics).
        seen: dict[tuple, dict] = {}
        total_trades = 0
        for pi, params in grid_entries:
            strategy = spec.factory(params)
            result = run_backtest(bars, strategy, config)
            try:
                params_repr = ",".join(
                    f"{f.name}={getattr(params, f.name)}"
                    for f in _dc_fields(params)
                )
            except TypeError:
                params_repr = repr(params)
            for tr in result.trades:
                total_trades += 1
                key = (tr.entry_time.isoformat(), tr.side.name)
                if key in seen and args.pool_grid:
                    continue
                seen[key] = {
                    "entry_time": tr.entry_time.isoformat(),
                    "exit_time": tr.exit_time.isoformat(),
                    "side": tr.side.name,
                    "entry_price": f"{tr.entry_price:.5f}",
                    "exit_price": f"{tr.exit_price:.5f}",
                    "stop": f"{tr.stop:.5f}",
                    "target": f"{tr.target:.5f}",
                    "pnl_r": f"{tr.pnl_r:+.4f}",
                    "bars_held": tr.bars_held,
                    "exit_reason": tr.exit_reason,
                    "reason": tr.reason,
                    "tags": "|".join(tr.tags),
                    "params_index": pi,
                    "params": params_repr,
                }

        rows = sorted(seen.values(), key=lambda r: r["entry_time"])
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        cols = [
            "entry_time", "exit_time", "side",
            "entry_price", "exit_price", "stop", "target",
            "pnl_r", "bars_held", "exit_reason", "reason", "tags",
            "params_index", "params",
        ]
        with outp.open("w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        wins = sum(1 for r in rows if float(r["pnl_r"]) > 0)
        avg_r = (
            sum(float(r["pnl_r"]) for r in rows) / len(rows)
            if rows else 0.0
        )
        print(
            f"{args.family}: grid_entries={len(grid_entries)} "
            f"total_trades={total_trades} unique_signals={len(rows)} "
            f"wins={wins} avg_r={avg_r:+.3f} -> {outp}"
        )
        return

    if args.command == "holdout-mined-pattern":
        # Inline holdout evaluation for a single pattern-miner survivor.
        # Bypasses the family registry — picks best (stop_atr, RR) on the
        # train slice, evaluates on the holdout slice, runs sign-randomization
        # permutation test. Tells us whether the forward-R edge survives
        # stop/target conversion (HANDBOOK lesson #2).
        from .strategies.mined_pattern import MinedPatternStrategy
        from .research.permutation import run_permutation_test
        from .backtest.metrics import summarize_backtest as _summ
        bars = load_bars_from_csv(args.csv_path)
        if not bars:
            raise SystemExit(f"no bars loaded from {args.csv_path}")
        feature_names = tuple(
            f.strip() for f in args.features.split(",") if f.strip()
        )
        if not feature_names:
            raise SystemExit("--features must be a comma-separated list")
        if args.direction not in ("long", "short"):
            raise SystemExit("--direction must be 'long' or 'short'")
        stop_atrs = [float(s) for s in args.stop_atrs.split(",") if s.strip()]
        risk_rewards = [float(s) for s in args.risk_rewards.split(",") if s.strip()]

        # Strict 75/25 train/holdout split — no peeking at the holdout.
        split = int(len(bars) * args.train_fraction)
        train_bars = bars[:split]
        holdout_bars = bars[split:]
        config = BacktestConfig(
            slippage_bps=args.slippage_bps,
            commission_per_trade=args.commission,
            fill_aware_stops=args.fill_aware_stops,
        )

        print(
            f"=== holdout-mined-pattern: {' & '.join(feature_names)} -> "
            f"{args.direction} ==="
        )
        print(
            f"bars={len(bars)} train={len(train_bars)} "
            f"holdout={len(holdout_bars)} grid={len(stop_atrs)*len(risk_rewards)}"
        )
        print(f"train range: {train_bars[0].timestamp} → {train_bars[-1].timestamp}")
        print(
            f"holdout range: {holdout_bars[0].timestamp} → "
            f"{holdout_bars[-1].timestamp}"
        )

        # ---- train sweep -----------------------------------------------
        print()
        print(f"{'stop_atr':>9} {'RR':>5} {'n_train':>8} "
              f"{'pf_train':>9} {'avg_r':>7}")
        print("-" * 45)
        best = None  # (pf, params, summary)
        for sa in stop_atrs:
            for rr in risk_rewards:
                strat = MinedPatternStrategy(
                    feature_names=feature_names,
                    direction=args.direction,
                    atr_period=args.atr_period,
                    stop_atr=sa,
                    risk_reward=rr,
                    max_spread=args.max_spread,
                )
                result = run_backtest(train_bars, strat, config)
                summary = _summ(result)
                pf = summary.profit_factor
                if pf == float("inf"):
                    pf_disp = 999.0
                else:
                    pf_disp = pf
                print(
                    f"{sa:>9.2f} {rr:>5.2f} {summary.total_trades:>8d} "
                    f"{pf_disp:>9.2f} {summary.average_r:>+7.3f}"
                )
                # Require >= min_train_trades to avoid PF=∞ on n=1
                if summary.total_trades < args.min_train_trades:
                    continue
                if best is None or pf > best[0]:
                    best = (pf, (sa, rr), summary)

        if best is None:
            print()
            print("FAIL: no train configuration produced ≥ "
                  f"{args.min_train_trades} trades — pattern too sparse "
                  "for stop/target conversion")
            return

        best_pf, (best_sa, best_rr), train_summary = best
        print()
        print(f"best train: stop_atr={best_sa} RR={best_rr} "
              f"PF={best_pf:.2f} n={train_summary.total_trades} "
              f"avg_r={train_summary.average_r:+.3f}")

        # ---- holdout evaluation ---------------------------------------
        ho_strat = MinedPatternStrategy(
            feature_names=feature_names,
            direction=args.direction,
            atr_period=args.atr_period,
            stop_atr=best_sa,
            risk_reward=best_rr,
            max_spread=args.max_spread,
        )
        ho_result = run_backtest(holdout_bars, ho_strat, config)
        ho_summary = _summ(ho_result)
        ho_pf = ho_summary.profit_factor
        ho_pf_disp = 999.0 if ho_pf == float("inf") else ho_pf
        print()
        print("--- holdout ---")
        print(
            f"trades={ho_summary.total_trades} "
            f"pf={ho_pf_disp:.2f} "
            f"avg_r={ho_summary.average_r:+.3f} "
            f"win_rate={ho_summary.win_rate:.1%} "
            f"max_dd={ho_summary.max_drawdown:.1%}"
        )

        # ---- permutation test ----------------------------------------
        perm = run_permutation_test(
            holdout_bars, ho_strat, config,
            n_permutations=args.n_permutations,
            seed=args.permutation_seed,
        )
        print()
        print("--- permutation test (sign randomization on holdout) ---")
        print(
            f"observed_pf={perm.observed_pf:.2f} "
            f"p_value={perm.p_value:.4f} "
            f"null_mean_pf={perm.null_mean_pf:.2f} "
            f"null_median_pf={perm.null_median_pf:.2f}"
        )
        print(f"verdict: {perm.verdict}")

        # Final tradable verdict — gates from HANDBOOK §10
        print()
        if ho_summary.total_trades < 30:
            verdict = (
                f"UNDERPOWERED: holdout n={ho_summary.total_trades} < 30 — "
                "edge cannot be confirmed at this n"
            )
        elif ho_pf_disp < 1.20:
            verdict = (
                f"FAIL: holdout PF={ho_pf_disp:.2f} < 1.20 — "
                "forward-R edge does not survive stop/target conversion"
            )
        elif perm.p_value > 0.20:
            verdict = (
                f"FAIL: permutation p={perm.p_value:.3f} > 0.20 — "
                "indistinguishable from random sign flips"
            )
        else:
            verdict = (
                f"PASS: holdout PF={ho_pf_disp:.2f} n={ho_summary.total_trades} "
                f"p={perm.p_value:.3f} — candidate edge worth pursuing"
            )
        print(f"FINAL: {verdict}")
        return

    parser.print_help()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gold trader research CLI")
    subparsers = parser.add_subparsers(dest="command")

    smoke = subparsers.add_parser("smoke", help="Run a deterministic smoke backtest")
    smoke.add_argument("--bars", type=int, default=500)
    smoke.add_argument("--seed", type=int, default=7)

    download = subparsers.add_parser(
        "download-dukascopy",
        help="Download real Dukascopy XAUUSD history and save it as a normalized CSV",
    )
    download.add_argument("--symbol", default="XAUUSD")
    download.add_argument("--start-date", required=True)
    download.add_argument("--end-date", required=True)
    download.add_argument("--interval-minutes", type=int, default=15)
    download.add_argument("--max-workers", type=int, default=1)
    download.add_argument("--price-decimals", type=int, default=None)
    download.add_argument("--output", required=True)

    sync = subparsers.add_parser(
        "sync-dukascopy",
        help="Download current Dukascopy data at a base timeframe and generate aligned higher timeframes",
    )
    sync.add_argument("--symbol", default="XAUUSD")
    sync.add_argument("--days", type=int, default=7)
    sync.add_argument("--end-date", default=None)
    sync.add_argument("--base-interval-minutes", type=int, default=1)
    sync.add_argument("--timeframes", default="1,5,15,60,240")
    sync.add_argument("--max-workers", type=int, default=1)
    sync.add_argument("--price-decimals", type=int, default=None)
    sync.add_argument("--output-dir", required=True)

    csv_parser = subparsers.add_parser("csv", help="Run a backtest against a normalized CSV")
    csv_parser.add_argument("csv_path")

    walk_forward = subparsers.add_parser("walk-forward", help="Run walk-forward summaries")
    walk_forward.add_argument("csv_path")
    walk_forward.add_argument("--train-size", type=int, required=True)
    walk_forward.add_argument("--test-size", type=int, required=True)
    walk_forward.add_argument("--step-size", type=int, default=None)

    sweep = subparsers.add_parser("sweep", help="Run a parameter sweep for the liquidity sweep strategy")
    sweep.add_argument("csv_path")
    sweep.add_argument("--lookbacks", default="15,20,25")
    sweep.add_argument("--atr-periods", default="14")
    sweep.add_argument("--min-sweep-atrs", default="0.2,0.3,0.4")
    sweep.add_argument("--risk-rewards", default="1.5,2.0,2.5")
    sweep.add_argument("--max-spreads", default="0.50,0.75,1.00")
    sweep.add_argument("--min-news-distances", default="30,60")
    sweep.add_argument("--max-workers", type=int, default=1)
    sweep.add_argument("--top", type=int, default=10)

    research = subparsers.add_parser(
        "research-bundle",
        help="Run a parallel multi-timeframe research pass across synchronized CSV datasets",
    )
    research.add_argument("data_dir")
    research.add_argument("--timeframes", default="5,15,60,240")
    research.add_argument("--families", default="asian_range_breakout,ny_session_breakout,momentum_burst")
    research.add_argument("--train-bars", type=int, default=120)
    research.add_argument("--test-bars", type=int, default=40)
    research.add_argument("--step-bars", type=int, default=40)
    research.add_argument("--min-trades", type=int, default=5)
    research.add_argument("--max-workers", type=int, default=0)
    research.add_argument("--top", type=int, default=20)

    analysis = subparsers.add_parser(
        "analyze-bundle",
        help="Generate a detailed multi-timeframe market analysis report and charts from synchronized CSV datasets",
    )
    analysis.add_argument("data_dir")
    analysis.add_argument("--timeframes", default="15,60,240,1440")
    analysis.add_argument("--output-dir", required=True)
    analysis.add_argument("--skip-charts", action="store_true")

    monitor = subparsers.add_parser(
        "monitor-bundle",
        help="Build a current multi-timeframe market-state snapshot and optionally repeat it",
    )
    monitor.add_argument("data_dir")
    monitor.add_argument("--timeframes", default="5,15,60,240,1440")
    monitor.add_argument("--families", default="asian_range_breakout,ny_session_breakout,momentum_burst")
    monitor.add_argument("--iterations", type=int, default=1)
    monitor.add_argument("--interval-seconds", type=int, default=60)
    monitor.add_argument("--max-candidates", type=int, default=8)

    agent_cycle = subparsers.add_parser(
        "agent-cycle",
        help="Refresh current Dukascopy data and emit a current market-state snapshot in a repeatable loop",
    )
    agent_cycle.add_argument("--symbol", default="XAUUSD")
    agent_cycle.add_argument("--days", type=int, default=7)
    agent_cycle.add_argument("--end-date", default=None)
    agent_cycle.add_argument("--base-interval-minutes", type=int, default=15)
    agent_cycle.add_argument("--timeframes", default="15,60,240,1440")
    agent_cycle.add_argument("--max-workers", type=int, default=1)
    agent_cycle.add_argument("--price-decimals", type=int, default=None)
    agent_cycle.add_argument("--output-dir", required=True)
    agent_cycle.add_argument("--families", default="asian_range_breakout,ny_session_breakout,momentum_burst")
    agent_cycle.add_argument("--iterations", type=int, default=1)
    agent_cycle.add_argument("--interval-seconds", type=int, default=300)
    agent_cycle.add_argument("--max-candidates", type=int, default=8)
    agent_cycle.add_argument("--paper-equity", type=float, default=10_000.0)
    agent_cycle.add_argument(
        "--risk-per-trade", type=float, default=0.01,
        help="Fraction of equity to risk per trade (default: 0.01 = 1%%)",
    )
    agent_cycle.add_argument(
        "--max-daily-trades", type=int, default=3,
        help="Maximum trades to open in a single UTC day (default: 3)",
    )

    perm = subparsers.add_parser(
        "permutation-test",
        help="Run a sign-randomization permutation test to assess whether a strategy has statistically significant edge",
    )
    perm.add_argument("csv_path", help="Path to a single-timeframe normalized CSV")
    perm.add_argument(
        "--family",
        default="asian_range_breakout",
        choices=[
            "liquidity_sweep", "compression_breakout", "asian_range_breakout",
            "london_breakout", "trend_pullback", "ny_session_breakout", "momentum_burst",
            "previous_day_breakout", "opening_range_breakout", "asian_range_fade",
            "fair_value_gap", "ny_close_compression", "session_continuation",
        ],
    )
    perm.add_argument("--n-permutations", type=int, default=10_000)
    perm.add_argument("--seed", type=int, default=42)

    holdout = subparsers.add_parser(
        "holdout-eval",
        help=(
            "Formal train/holdout evaluation: fit parameters on the train portion, "
            "evaluate strictly on the held-out period, run permutation test on held-out trades"
        ),
    )
    holdout.add_argument("csv_path", help="Path to a single-timeframe normalized CSV")
    holdout.add_argument(
        "--family",
        default="asian_range_breakout",
        choices=[
            "liquidity_sweep", "compression_breakout", "asian_range_breakout",
            "london_breakout", "trend_pullback", "ny_session_breakout", "momentum_burst",
            "previous_day_breakout", "opening_range_breakout", "asian_range_fade",
            "fair_value_gap", "ny_close_compression", "session_continuation",
            "dxy_lead_lag", "real_yield_reversal", "timed_horizon_macro_regime",
            "rsi_divergence", "inversion_fair_value_gap",
        ],
    )
    holdout.add_argument("--holdout-fraction", type=float, default=1 / 3,
                         help="Fraction of bars to reserve as held-out (default: 1/3)")
    holdout.add_argument("--min-train-trades", type=int, default=5)
    holdout.add_argument("--n-permutations", type=int, default=5_000)
    holdout.add_argument(
        "--workers", type=int, default=12,
        help="Worker processes for parallel param search (default: 12, uses pool initializer pattern)",
    )
    holdout.add_argument(
        "--grid-sample", type=int, default=0,
        help="If >0, randomly sample this many parameter combos from the full grid (deterministic seed). "
             "Use to keep the search tractable on large grids; 0 = use full grid.",
    )
    holdout.add_argument(
        "--grid-sample-seed", type=int, default=42,
        help="Seed for --grid-sample (default: 42)",
    )
    holdout.add_argument(
        "--skip-walk-forward", action="store_true",
        help="Skip the inner true-walk-forward parameter scan (saves ~50% of CPU). "
             "Selects best params via a single full-train backtest only.",
    )
    holdout.add_argument(
        "--quick", action="store_true",
        help="Preset for fast iteration: --grid-sample 64 --skip-walk-forward --n-permutations 500.",
    )
    holdout.add_argument(
        "--macro-cache-dir", default="data/macro",
        help="Macro CSV cache directory (used by family=real_yield_reversal "
             "and family=timed_horizon_macro_regime). Default: data/macro",
    )

    mine = subparsers.add_parser(
        "mine-patterns",
        help=(
            "Exhaustively scan the boolean feature vocabulary for "
            "1- and 2-feature conjunctions whose forward N-bar return "
            "differs from zero, with Benjamini-Hochberg FDR control "
            "and a strict train/holdout split."
        ),
    )
    mine.add_argument("csv_path", help="Path to a normalized bar CSV")
    mine.add_argument("--horizon", type=int, default=8,
                      help="Forward-return horizon in bars (default: 8)")
    mine.add_argument("--max-combo-size", type=int, default=2,
                      help="Max features per conjunction (default: 2)")
    mine.add_argument("--min-signals", type=int, default=50,
                      help="Min train-side signals per pattern (default: 50)")
    mine.add_argument("--min-effect-r", type=float, default=0.10,
                      help="Min |mean forward-R| in ATR units (default: 0.10)")
    mine.add_argument("--fdr-q", type=float, default=0.10,
                      help="Benjamini-Hochberg FDR target (default: 0.10)")
    mine.add_argument("--bootstrap-blocks", type=int, default=1000,
                      help="Block-bootstrap iterations per pattern (default: 1000)")
    mine.add_argument("--block-size", type=int, default=16,
                      help="Block-bootstrap block size in bars (default: 16)")
    mine.add_argument("--train-fraction", type=float, default=2 / 3,
                      help="Fraction of bars for training (default: 2/3)")
    mine.add_argument("--top", type=int, default=20,
                      help="Show this many top survivors (default: 20)")
    mine.add_argument("--output", default=None,
                      help="Optional CSV file to dump all survivors")

    mine_all = subparsers.add_parser(
        "mine-all",
        help=(
            "Multi-timeframe, multi-horizon pattern sweep. "
            "Resamples a base CSV (typically 15m) up to 60m/240m/1440m, "
            "mines each at multiple forward horizons, and reports "
            "feature combinations that replicate across timeframes."
        ),
    )
    mine_all.add_argument("csv_path", help="Path to a base normalized bar CSV (e.g. xauusd_full_15m.csv)")
    mine_all.add_argument("--timeframes", default="15,60,240,1440",
                          help="Comma-separated timeframes in minutes (default: 15,60,240,1440)")
    mine_all.add_argument("--horizons", default="4,8,16",
                          help="Comma-separated forward-return horizons in bars (default: 4,8,16)")
    mine_all.add_argument("--max-combo-size", type=int, default=2,
                          help="Max features per conjunction (default: 2; set 3 for deeper search)")
    mine_all.add_argument("--min-signals", type=int, default=80,
                          help="Train-side signal floor (auto-relaxed for small TFs; default: 80)")
    mine_all.add_argument("--min-effect-r", type=float, default=0.10)
    mine_all.add_argument("--fdr-q", type=float, default=0.10)
    mine_all.add_argument("--bootstrap-blocks", type=int, default=500)
    mine_all.add_argument("--block-size", type=int, default=16,
                          help="Block-bootstrap block size in bars (auto-clamped per TF; default: 16)")
    mine_all.add_argument("--train-fraction", type=float, default=2 / 3)
    mine_all.add_argument("--output-dir", default="reports/mined_patterns/sweep",
                          help="Directory to write all_survivors.csv and cross_tf_replicators.csv")
    mine_all.add_argument("--with-macro", action="store_true",
                          help="Augment feature matrix with macro-regime features (yields, DXY, VIX, etc.)")
    mine_all.add_argument("--macro-cache-dir", default="data/macro",
                          help="Macro CSV cache directory (default: data/macro)")

    combine = subparsers.add_parser(
        "combine-csv",
        help="Merge multiple normalized bar CSVs into one, sorted and deduplicated by timestamp",
    )
    combine.add_argument(
        "inputs",
        nargs="+",
        help="Input CSV file paths (glob patterns accepted, e.g. 'data/xauusd_y*/*_15m.csv')",
    )
    combine.add_argument("--output", required=True, help="Output CSV file path")

    extend = subparsers.add_parser(
        "extend-csv",
        help=(
            "Incrementally extend an existing CSV with new bars from Dukascopy. "
            "Reads the last bar timestamp from the file and only fetches the gap to --end-date. "
            "Creates the file from scratch if it does not exist (requires --start-date)."
        ),
    )
    extend.add_argument("csv_path", help="Path to the target CSV (will be created or appended)")
    extend.add_argument("--symbol", default="XAUUSD")
    extend.add_argument("--start-date", default=None, help="Required only when creating a new file")
    extend.add_argument("--end-date", default=None, help="Defaults to today UTC")
    extend.add_argument("--interval-minutes", type=int, default=15)
    extend.add_argument("--max-workers", type=int, default=4)
    extend.add_argument("--price-decimals", type=int, default=None)

    merge_dxy = subparsers.add_parser(
        "merge-dxy",
        help=(
            "Populate the dxy_close column in a XAUUSD CSV using an EURUSD CSV as a proxy. "
            "EUR/USD has >0.97 correlation with the DXY index. The proxy is normalised to 100 "
            "at the first overlapping bar. Rewrites the XAUUSD CSV in-place (or to --output)."
        ),
    )
    merge_dxy.add_argument("xauusd_csv", help="Path to the XAUUSD bars CSV to enrich")
    merge_dxy.add_argument("eurusd_csv", help="Path to a EURUSD bars CSV (any timeframe)")
    merge_dxy.add_argument("--output", default=None, help="Write enriched CSV here instead of overwriting xauusd_csv")

    sync_macro = subparsers.add_parser(
        "sync-macro",
        help=(
            "Fetch the cross-asset macro bundle (DXY, US10Y/2Y, real 10Y yield, VIX, SPX, USDJPY) "
            "from FRED and Stooq.  Results cached as data/macro/<name>.csv.  Idempotent."
        ),
    )
    sync_macro.add_argument("--cache-dir", default="data/macro", help="Where to write the per-series CSVs")
    sync_macro.add_argument("--start-date", default=None, help="ISO date (YYYY-MM-DD).  Defaults to end-date minus --lookback-days.")
    sync_macro.add_argument("--end-date", default=None, help="ISO date (YYYY-MM-DD).  Defaults to today (UTC).")
    sync_macro.add_argument("--lookback-days", type=int, default=730, help="Used only when --start-date is omitted (default: 730 = ~2y).")
    sync_macro.add_argument("--names", default=None, help="Comma-separated subset of macro names; default: all")
    sync_macro.add_argument("--refresh", action="store_true", default=True, help="Re-download even if cached (default true)")
    sync_macro.add_argument("--no-refresh", dest="refresh", action="store_false", help="Use cache when present, only fetch missing")

    show_macro = subparsers.add_parser(
        "show-macro",
        help="Print a one-line summary of each cached macro series (date range, count, first/last value).",
    )
    show_macro.add_argument("--cache-dir", default="data/macro")

    paper_report = subparsers.add_parser(
        "paper-report",
        help="Print a summary of paper trading performance from a saved paper_state.json",
    )
    paper_report.add_argument(
        "state_path",
        help="Path to paper_state.json",
    )

    broker_info = subparsers.add_parser(
        "broker-info",
        help="Print broker account info + any open position; broker chosen via GOLD_BROKER env var",
    )
    broker_info.set_defaults()  # no extra args; reads env

    panic = subparsers.add_parser(
        "panic",
        help="EMERGENCY: cancel all pending and close all open positions under our magic",
    )
    panic.add_argument(
        "--db-path",
        default=None,
        help="Path to state.db (defaults to <output-dir>/state.db then data/state.db)",
    )
    panic.add_argument(
        "--output-dir",
        default=None,
        help="Optional output directory used to locate state.db",
    )

    dashboard = subparsers.add_parser(
        "dashboard",
        help="Render an offline HTML dashboard of paper state + broker info",
    )
    dashboard.add_argument(
        "state_path",
        help="Path to paper_state.json",
    )
    dashboard.add_argument(
        "--output",
        default="reports/dashboard/index.html",
        help="HTML output path (default reports/dashboard/index.html)",
    )
    dashboard.add_argument(
        "--starting-equity",
        type=float,
        default=10000.0,
    )
    dashboard.add_argument(
        "--title",
        default="Gold Trader — Live Paper Dashboard",
    )
    dashboard.add_argument(
        "--no-broker",
        action="store_true",
        help="Skip live broker query (offline rendering only)",
    )

    calibrate = subparsers.add_parser(
        "calibrate-scores",
        help="Bin backtest trades by score bracket to validate whether higher scores predict better outcomes",
    )
    calibrate.add_argument("csv_path", help="Path to a single-timeframe normalized CSV")
    calibrate.add_argument(
        "--family",
        default="asian_range_breakout",
        choices=[
            "liquidity_sweep", "compression_breakout", "asian_range_breakout",
            "london_breakout", "trend_pullback", "ny_session_breakout", "momentum_burst",
            "previous_day_breakout", "opening_range_breakout", "asian_range_fade",
        ],
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the local web control panel (http://127.0.0.1:8770) — note: avoid 8765 which is the MT5 bridge default",
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8770)

    slicer = subparsers.add_parser(
        "slice-probabilities",
        help=(
            "Build a conditional-probability table for a strategy: slices every "
            "historical signal by regime/session/macro/time and reports n, "
            "win-rate, expectancy, and a lower-bound CI per slice. Surfaces "
            "the exact conditions under which the strategy works."
        ),
    )
    slicer.add_argument("csv_path", help="Path to a normalized bar CSV")
    slicer.add_argument(
        "--family",
        default="all",
        help="Strategy family (or 'all' for every self-contained family)",
    )
    slicer.add_argument(
        "--params-index", type=int, default=0,
        help="Which entry of the family's default grid to use (default: 0)",
    )
    slicer.add_argument("--output-dir", default="config/probability_tables")
    slicer.add_argument("--no-pairs", action="store_true",
                        help="Skip 2-dimensional slices (faster)")
    slicer.add_argument("--pool-grid", action="store_true",
                        help="Pool trades across the entire family grid (grows n "
                             "for selective strategies like rsi_divergence/ifvg)")
    slicer.add_argument("--min-pair-n", type=int, default=8)
    slicer.add_argument("--min-n", type=int, default=20,
                        help="Edge-slice gate: minimum trades (default 20 since 2026-05-09)")
    slicer.add_argument("--min-expectancy-r", type=float, default=0.10,
                        help="Edge-slice gate: minimum R-expectancy")
    slicer.add_argument("--min-profit-factor", type=float, default=1.20,
                        help="Edge-slice gate: minimum profit factor")
    slicer.add_argument("--show-top", type=int, default=15)
    slicer.add_argument("--slippage-bps", type=float, default=0.0)
    slicer.add_argument("--commission", type=float, default=0.0)
    slicer.add_argument("--fill-aware-stops", action="store_true")

    dump = subparsers.add_parser(
        "dump-signals",
        help=(
            "Dump every fired signal of a strategy to CSV (one row per "
            "closed trade). Use to eyeball whether the encoded rule fires "
            "on the same bars the operator would — pattern-recognition "
            "ground-truth check before holdout-eval."
        ),
    )
    dump.add_argument("csv_path", help="Path to a normalized bar CSV")
    dump.add_argument("--family", required=True,
                      help="Strategy family (self-contained registry)")
    dump.add_argument("--output", required=True,
                      help="Output CSV path for the signal dump")
    dump.add_argument("--params-index", type=int, default=0,
                      help="Which entry of the family's grid to use (default: 0)")
    dump.add_argument("--pool-grid", action="store_true",
                      help="Run every grid combo and dedupe by (entry_time, side)")
    dump.add_argument("--slippage-bps", type=float, default=0.0)
    dump.add_argument("--commission", type=float, default=0.0)
    dump.add_argument("--fill-aware-stops", action="store_true")

    holdout_mined = subparsers.add_parser(
        "holdout-mined-pattern",
        help=(
            "Convert a pattern-miner survivor (e.g. 'hour_o7,range_q0' long) "
            "into a stop/target trade rule and holdout-eval it. Sweeps a "
            "small (stop_atr × RR) grid on train, picks best PF, runs "
            "permutation test on holdout. Tests whether forward-R edges "
            "survive stop/target conversion (HANDBOOK lesson #2)."
        ),
    )
    holdout_mined.add_argument("csv_path", help="Path to a normalized bar CSV")
    holdout_mined.add_argument("--features", required=True,
                               help="Comma-separated feature names (must all be "
                                    "True at the entry bar). E.g. 'hour_o7,range_q0'")
    holdout_mined.add_argument("--direction", default="long",
                               choices=["long", "short"])
    holdout_mined.add_argument("--atr-period", type=int, default=14)
    holdout_mined.add_argument("--stop-atrs", default="0.5,1.0,1.5,2.0",
                               help="Comma-separated stop multiples in ATRs")
    holdout_mined.add_argument("--risk-rewards", default="1.0,1.5,2.0,3.0",
                               help="Comma-separated risk:reward ratios")
    holdout_mined.add_argument("--max-spread", type=float, default=1.0)
    holdout_mined.add_argument("--train-fraction", type=float, default=0.75)
    holdout_mined.add_argument("--min-train-trades", type=int, default=30)
    holdout_mined.add_argument("--n-permutations", type=int, default=2000)
    holdout_mined.add_argument("--permutation-seed", type=int, default=42)
    holdout_mined.add_argument("--slippage-bps", type=float, default=0.0)
    holdout_mined.add_argument("--commission", type=float, default=0.0)
    holdout_mined.add_argument("--fill-aware-stops", action="store_true")

    # ------------------------------------------------------------------
    # Concurrence-gated ensemble (walk-forward validated entry point)
    # ------------------------------------------------------------------
    check_conc = subparsers.add_parser(
        "check-concurrence",
        help=(
            "Live diagnostic: at the last bar of a CSV (or --at <iso>), "
            "report which strategies are firing and the concurrence count. "
            "Walk-forward validated alpha threshold is >= 5."
        ),
    )
    check_conc.add_argument("csv_path", help="Normalized single-timeframe CSV")
    check_conc.add_argument(
        "--at",
        default=None,
        help="Optional ISO timestamp; defaults to the last bar in the CSV",
    )
    check_conc.add_argument(
        "--min", dest="gate_min", type=int, default=5,
        help="Concurrence threshold to flag as ARMED (default 5)",
    )

    ensemble_bt = subparsers.add_parser(
        "ensemble-backtest",
        help=(
            "Run a concurrence-gated ensemble backtest across all "
            "self-contained strategy families on default params. Trades "
            "are taken only on bars where >= --gate-min strategies fire "
            "the same side."
        ),
    )
    ensemble_bt.add_argument("csv_path", help="Normalized single-timeframe CSV")
    ensemble_bt.add_argument("--gate-min", type=int, default=5)
    ensemble_bt.add_argument("--slippage-bps", type=float, default=0.0)
    ensemble_bt.add_argument("--commission", type=float, default=0.0)
    ensemble_bt.add_argument("--fill-aware-stops", action="store_true")
    ensemble_bt.add_argument(
        "--weights",
        default="data/strategy_weights.json",
        help="Path to strategy_weights.json used for tie-breaking when "
             "multiple strategies fire the same bar (default "
             "data/strategy_weights.json; missing file -> alphabetical).",
    )
    ensemble_bt.add_argument(
        "--events-csv",
        default=None,
        help="Optional path to write per-event diagnostics CSV",
    )

    return parser


def _format_summary(summary, title: str) -> str:
    lines = [title]
    lines.append(f"  trades: {summary.total_trades}")
    lines.append(f"  win_rate: {summary.win_rate:.2%}")
    lines.append(f"  average_r: {summary.average_r:.3f}")
    lines.append(f"  profit_factor: {summary.profit_factor:.3f}")
    lines.append(f"  max_drawdown: {summary.max_drawdown:.2%}")
    lines.append(f"  total_pnl: {summary.total_pnl:.2f}")
    lines.append(f"  total_return: {summary.total_return:.2%}")
    lines.append(f"  ending_equity: {summary.ending_equity:.2f}")
    lines.append(f"  kill_switch: {summary.halted_by_kill_switch}")
    return "\n".join(lines)


def _parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_float_list(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw.strip())


def _parse_family_list(raw: str) -> list[str]:
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _validate_syncable_timeframes(base_interval_minutes: int, timeframes: list[int]) -> None:
    if base_interval_minutes <= 0:
        raise ValueError("base_interval_minutes must be positive")
    if not timeframes:
        raise ValueError("timeframes must not be empty")
    for interval_minutes in timeframes:
        if interval_minutes <= 0:
            raise ValueError("timeframes must be positive")
        if interval_minutes < base_interval_minutes:
            raise ValueError("timeframes must be greater than or equal to the base interval")
        if interval_minutes % base_interval_minutes != 0:
            raise ValueError("timeframes must be multiples of the base interval")


def _timeframe_filename(symbol: str, start_date: date, end_date: date, interval_minutes: int) -> str:
    return (
        f"{symbol.lower()}_{start_date.isoformat()}_{end_date.isoformat()}_{interval_minutes}m.csv"
    )


def _format_research_result(rank, result) -> str:
    lines = [
        (
            f"Rank {rank} family={result.family} timeframe={result.timeframe_minutes}m "
            f"{result.parameter_text}"
        )
    ]
    lines.append(f"  trades: {result.summary.total_trades}")
    lines.append(f"  win_rate: {result.summary.win_rate:.2%}")
    lines.append(f"  average_r: {result.summary.average_r:.3f}")
    lines.append(f"  profit_factor: {result.summary.profit_factor:.3f}")
    lines.append(f"  max_drawdown: {result.summary.max_drawdown:.2%}")
    lines.append(f"  total_return: {result.summary.total_return:.2%}")
    lines.append(f"  walk_forward_windows: {result.walk_forward.window_count}")
    lines.append(f"  walk_forward_positive_ratio: {result.walk_forward.positive_window_ratio:.2%}")
    lines.append(f"  walk_forward_average_r: {result.walk_forward.average_r:.3f}")
    lines.append(f"  walk_forward_average_return: {result.walk_forward.average_return:.2%}")
    lines.append(f"  walk_forward_worst_drawdown: {result.walk_forward.worst_drawdown:.2%}")
    return "\n".join(lines)


def _format_analysis_profile(profile) -> str:
    lines = [
        (
            f"Timeframe {profile.timeframe_minutes}m trend={profile.trend_state} "
            f"return={profile.total_return:.2%} bars={profile.bar_count}"
        )
    ]
    lines.append(f"  rsi14: {profile.rsi14:.2f}")
    lines.append(f"  atr14: {profile.atr14:.3f}")
    lines.append(f"  trend_strength: {profile.trend_strength:.3f}")
    lines.append(f"  spread_mean: {profile.spread_mean:.3f}")
    lines.append(f"  spread_max: {profile.spread_max:.3f}")
    lines.append(
        f"  breakouts_up_down: {profile.donchian_breakout_up_count}/{profile.donchian_breakout_down_count}"
    )
    lines.append(
        f"  sweeps_up_down: {profile.liquidity_sweep_up_count}/{profile.liquidity_sweep_down_count}"
    )
    lines.append(f"  best_session: {profile.best_session}")
    lines.append(f"  worst_session: {profile.worst_session}")
    return "\n".join(lines)


def _format_bundle_snapshot(snapshot, iteration: int) -> str:
    lines = [f"Snapshot {iteration} @ {snapshot.generated_at.isoformat()}"]
    lines.append(f"  alignment: {snapshot.alignment_label}")
    lines.append(f"  higher_timeframe_bias: {snapshot.higher_timeframe_bias}")
    lines.append(f"  oscillation_label: {snapshot.oscillation_label}")
    lines.append(
        f"  decision: {snapshot.decision.status} family={snapshot.decision.family} timeframe={snapshot.decision.timeframe_minutes} score={snapshot.decision.score} rr={snapshot.decision.risk_reward:.2f}"
    )
    lines.append("  timeframe_states:")
    for state in snapshot.timeframe_states:
        lines.append(
            (
                f"    {state.timeframe_minutes}m trend={state.trend_state} structure={state.structure_state} "
                f"style={state.execution_style} close={state.current_close:.2f} "
                f"support={state.recent_support:.2f} resistance={state.recent_resistance:.2f} "
                f"rsi14={state.rsi14:.2f} atr14={state.atr14:.2f} spread={state.spread:.3f}"
            )
        )
    if snapshot.entry_candidates:
        lines.append("  entry_candidates:")
        for candidate in snapshot.entry_candidates:
            conflict_text = f" conflict={candidate.conflict}" if candidate.conflict else ""
            manual_text = ""
            if candidate.details and candidate.details.get("manual_approval_required"):
                manual_text = " manual_approval_required=true"
            lines.append(
                (
                    f"    {candidate.family} {candidate.timeframe_minutes}m {candidate.side.value} score={candidate.score} "
                    f"entry={candidate.reference_price:.2f} stop={candidate.stop:.2f} target={candidate.target:.2f} "
                    f"fit={candidate.regime_fit}{conflict_text}{manual_text}"
                )
            )
            lines.append(f"      reason: {candidate.reason}")
            if candidate.details and candidate.family == "inversion_fair_value_gap":
                plan = candidate.details.get("entry_plan", {})
                lines.append(
                    "      IFVG plan: "
                    f"zone={candidate.details.get('zone', {}).get('bot', 0):.2f}-"
                    f"{candidate.details.get('zone', {}).get('top', 0):.2f} "
                    f"entry={plan.get('entry_low', 0):.2f}-{plan.get('entry_high', 0):.2f} "
                    f"SL={plan.get('stop', 0):.2f} TP1={plan.get('tp1', 0):.2f} "
                    f"TP2={plan.get('tp2', 0):.2f} TP3={plan.get('tp3', 0):.2f}"
                )
    else:
        lines.append("  entry_candidates: none")
    if snapshot.warnings:
        lines.append("  warnings:")
        for warning in snapshot.warnings:
            lines.append(f"    - {warning}")
    lines.append("  decision_rationale:")
    for rationale in snapshot.decision.rationale:
        lines.append(f"    - {rationale}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()