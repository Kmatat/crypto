# Changelog

Release numbers follow `major.minor.patch`: a new **minor** number for new abilities, a new **patch** number for
fixes only, and a new **major** number only if the owner has to do something by hand (such as re-running the setup).
The number lives in `trader/__init__.py`; `/version` on Telegram and `desk version` on the server show it next to
the exact code revision, and every update message names the version it installed.

## 1.3.2 - 2026-10-25

- The automated tests that guard every update finish about four times faster, so they fit the small free
  server's time limit: indicators are computed with the same arithmetic but much less overhead, the backtester
  and the laboratory compute each coin's indicators once and share them across every window and strategy card
  (the weekly lab benefits too), and the tests exam shorter windows of the same shape.
- The updater allows 25 minutes for the tests and the Telegram rejection message now says which test failed,
  so a failed update can be diagnosed from the phone.
- New installs run the update job at low priority so the trading desk keeps its pace while tests run.

## 1.3.1 - 2026-10-25

- Fix: the live desk now decides on finished candles only, as the backtester and the laboratory do; the
  still-forming candle supplies only the live price for stops, trailing and sizing. Until now the running
  desk was a more trade-happy variant of the measured strategy (about twice the buy signals at 15-minute checks).
- Fix: a coin remainder smaller than the exchange's step size (left when the buy fee is charged in the coin and
  the protective order sells the rounded amount) no longer stays behind as an unsellable, unprotectable position
  that blocked the coin, a position slot and the normal check pace; such dust counts as closed, and old dust is
  written off at the next check.
- Fix: the "AI budget used up" alert quotes the cap actually in force, and the server guide says 10% per coin.
- The Market Scout excludes every Binance tokenized stock or ETF (`<TICKER>B` names), not only a fixed list.
- README: the measured edge is statistically indistinguishable from zero; expectations set accordingly.

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
