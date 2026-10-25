# Changelog

Release numbers follow `major.minor.patch`: a new **minor** number for new abilities, a new **patch** number for
fixes only, and a new **major** number only if the owner has to do something by hand (such as re-running the setup).
The number lives in `trader/__init__.py`; `/version` on Telegram and `desk version` on the server show it next to
the exact code revision, and every update message names the version it installed.

## 1.4.2 - 2026-10-25

- Fix: the "one rule refused every buy signal" alarm judged the last 24 hours, so right after 1.4.1 corrected the
  old take-profit it reported the refusals made before the correction. It now judges only decisions made under
  the strategy and settings in force; `/health` shows those separately ("Since the strategy in force took effect").

## 1.4.1 - 2026-10-25

- Fix (the reason no trades happened): settings files written before 1.3.0 pinned `TAKE_PROFIT_ATR=3.0`, the
  default of the time. With the 2 ATR stop that is 1.5:1, and the checklist's 2:1 reward rule refused every
  buy. Such a value is now treated as the old default and replaced by 4.0 at start (kept if you also set
  `MIN_REWARD_RISK` to 1.5 or less, which means you chose it); the strategy in force follows; Telegram tells
  you once; `desk setup` saves it to the file. The settings file format is now version 3.
- Settings that make trading impossible (a target the reward rule can never accept, a position below the $10
  minimum) are reported at start and in `/health`; the laboratory refuses strategy cards with such a target.
- One refusal rule blocking every buy signal for a day now triggers its own alert, within hours.
- `/health`: is everything working - running version, last market check, errors, settings problems, update
  state, and what happened to every buy signal in the last 24 hours (the decision funnel).
- `/agents [days]`: the 14 agents and what each did (working, idle, off, failing), the strategy in force, and the
  strategies created, examined, promoted, rejected or rolled back, plus the coins added or removed.
- A pulse every `PULSE_HOURS` (default 6): checks done, buy signals and what happened to them, errors. A silent
  phone now always means something is wrong.
- Watchdog: the update job (every 30 minutes, a separate process) alerts when the desk has stopped checking the
  market, and restarts it if it stays stuck.
- Update messages are reliable: the job reports every outcome, including a crash or the server's time limit
  stopping it; an interrupted update is finished or undone by the next check; after a restart it waits until
  the new desk confirms its version; and the desk itself confirms "Update confirmed: now running version X".
- `/version` says when newer code is installed but the desk has not restarted onto it yet. Commands sent while
  the desk was restarting are answered (if under 15 minutes old) instead of being dropped.

## 1.4.0 - 2026-10-25

- Buy and sell thresholds are no longer one fixed number for every coin. A strategy card can carry a
  threshold per coin, within the same bounds, counted as one change for the laboratory's two-change rule.
- The threshold study (`trader/calibration.py`): every week the head trader replays each watched coin at
  every allowed threshold over a year of history that stops 90 days ago, picks the threshold with the best
  after-fee result, pulls it towards the common threshold in proportion to how little evidence the coin
  produced, and hands the result to the laboratory as a challenger. The exam's most recent window is exactly
  the held-out period; the forward test and the owner's approval follow as for any change. With too few
  trades the study changes nothing, and says so.
- The Head Trader agent (`trader/agents/head.py`) manages the other agents: it decides which coins may consult
  Claude at each check (positions at risk first, then the strongest signals) and spreads the day's AI budget
  over the whole day; it raises a coin's threshold when the desk already holds a coin that moves with it (the
  same bet twice); it tilts for news only once the scorecard shows news scores predict anything; and it hands
  every AI prompt the desk's playbook (strategy, thresholds, lessons, scorecard verdict, last review, lineage).
  It cannot change caps, brakes, the AI budget, the trading mode or promote a strategy.
- `/playbook` on Telegram shows the playbook and how the AI budget was spent at the last check; the notebook
  shows the threshold study and the threshold used for every trade; `python -m trader backtest --coin-buy
  BTC=0.4,ETH=0.3` replicates cards with per-coin thresholds. `HEAD_TRADER=false` switches the agent off.

## 1.3.3 - 2026-10-25

- Fix: two tests read the real `.env` of the machine they ran on, so the update check failed on any server
  whose settings were written by the current setup wizard (its `SETTINGS_VERSION=2` line changed what the
  test expected). Every test now sees an empty settings file; the server's configuration is never read by tests.

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
