PAPER JOURNAL AND PERFORMANCE
=============================

What gets journaled
- Every normalized decision from `logs/ifvg_mtf_decision_state.json` is appended
  to `logs/decision_journal.jsonl` as a single-line JSON record and the latest
  snapshot is written to `logs/latest_decision_snapshot.json`.
- Fields recorded include: timestamp, symbol, action, side, grade, score,
  entry/stop/tp references, spread, session, macro/sentiment/volatility state,
  and `paper_allowed`/`live_allowed` flags.

What counts as a paper signal
- A row in `logs/decision_journal.jsonl` qualifies for paper tracking when
  either its `action` contains `TRADE_READY` or `paper_allowed == true` and the
  `score` meets the policy threshold (loaded from `config/execution_policy.json`'s
  `minimum_final_score`, default 70).

How outcomes are measured
- `scripts/update_paper_signal_outcomes.py` scans the journal for qualifying
  rows and tracks each signal by polling bridge candles (or TwelveData/csv
  fallback) after the signal timestamp.
- For each signal we track TP1/TP2/TP3 and SL hits using candle high/low.
- We record `status` (open|tp1_hit|tp2_hit|tp3_hit|sl_hit|expired), the first
  outcome observed, `max_favorable_r`, and `max_adverse_r` (R = entry→SL).
- Results are persisted to `logs/paper_signal_outcomes.jsonl` one JSON record
  per tracked signal.

How R metrics are computed
- R is defined as the absolute price distance from `entry_reference` to the
  `stop_loss`.
- Favorable R is movement toward a TP expressed in multiples of R
  (e.g., 1.0 = TP distance equals R). Adverse R is the movement against the
  trade expressed the same way.

Where files are stored
- `logs/decision_journal.jsonl` — historical decisions (append-only)
- `logs/latest_decision_snapshot.json` — latest normalized decision
- `logs/paper_signal_outcomes.jsonl` — per-signal paper-tracking state
- `logs/paper_performance_report.json` and `.md` — aggregated performance

How to reset the paper journal
- Stop the scout (`bash scripts/stop.sh`) then remove/rotate the files in `logs/`:

```bash
rm logs/decision_journal.jsonl logs/paper_signal_outcomes.jsonl logs/paper_performance_report.*
```

Notes
- This pipeline is intentionally paper-only. It does not place live orders or
  change execution policy. Keys and credentials are unchanged by these scripts.
