# Changelog

Release numbers follow `major.minor.patch`: a new **minor** number for new abilities, a new **patch** number for
fixes only, and a new **major** number only if the owner has to do something by hand (such as re-running the setup).
The number lives in `trader/__init__.py`; `/version` on Telegram and `desk version` on the server show it next to
the exact code revision, and every update message names the version it installed.

## 1.3.0 - 2026-10-25

- The notebook: `data/notes/` holds every strategy, trade, lesson, event and result as Markdown pages, rebuilt
  after every market check; `/notes` sends it as a zip, the weekly backup brings it too, `desk notes` rebuilds it.
- `/review`: Claude reads the notebook and writes a plain-language review (advisory only).
- The research agent receives the lineage of every strategy already tried, so proposals build on what passed.
- Release numbers, this changelog, and update messages that name the version.
- Server guide: one-time upgrade steps for a server installed before automatic updates.
- Fix: `/lab` and `/review` may use the journal from their own thread.

## 1.2.0 - 2026-10-25

- The strategy laboratory: strategy cards within hard bounds, weekly research proposals, the 10-test exam,
  forward tests, owner approval (`/strategies`, `/approve`, `/reject`, `/lab`), automatic rollback.
- Master traders' checklist (trend, Bitcoin regime, cooling-off, reward-to-risk), trailing stops, Turtle sizing
  that can only shrink positions, loss brakes with a 14-day pause and half-size recovery.
- Backtester on real Binance history (`python -m trader backtest`); demo goals and benchmarks in the reports.
- Claude becomes veto-only and is measured by a scorecard computed from real later prices; news weight reduced;
  AI budget capped at a percentage of the account.
- Security audit fixes: sandboxed updater, locked dependency versions, safer limits.

## 1.1.0 - 2026-10-24

- Checks every 15 minutes with an AI-set pace; the Market Scout adds coins by research.
- Stop-loss and take-profit placed on Binance as an OCO pair, with emulation in paper mode.
- Secrets handling, Telegram pairing and the server service hardened.
- Over-the-air updates: tested before install, rolled back on failure, `/update` and `/version` on Telegram.

## 1.0.0 - 2026-10-24

- First server release: news, forex, technical, prediction, decision, risk and execution agents; Binance Spot
  Testnet orders; SQLite journal and reports; Telegram alerts and commands; setup wizard; free Oracle server
  guide and one-command installer.
