# Changelog

Release numbers follow `major.minor.patch`: a new **minor** number for new abilities, a new **patch** number for
fixes only, and a new **major** number only if the owner has to do something by hand (such as re-running the setup).
The number lives in `trader/__init__.py`; `/version` on Telegram and `desk version` on the server show it next to
the exact code revision, and every update message names the version it installed.

## 1.6.3 - 2026-10-26

Fixes found while reading the owner's first day of real records at $5,000:
- `/diagnose` compared the desk's record with a replay of the full period asked (7 days by default) even when the
  record was one day old, and reported "missing" trades that were simply never recorded. The period is now never
  longer than the record, and the title says so.
- The rules' replay in `/diagnose` downloaded only the period itself, so the checklist's 200-day average could not be
  computed and counted as "unknown" (which does not block): the replay used looser rules than the live desk and
  overstated the trades its rules "would have made". It now downloads 230 days before the period.
- "Demo vs real price gap" judged a stop-loss or take-profit filled by the exchange against the price at the desk's
  next check, up to 30 minutes later, so ordinary market moves counted as a bad fill. Exchange exits are now judged
  against the price the order was set at, and the desk's own orders against the real price read just before sending.
- Wording: a Claude budget with no daily cap reads "no daily cap" (not "$0.00/day cap") and says how to set one; the
  head trader says "no coin needed an AI opinion" instead of "up to 0 question(s)".

## 1.6.2 - 2026-10-26

- Fix: a budget changed after the desk's record began (STARTING_BALANCE_USDT) never took effect. The record keeps the
  budget it started with, and position sizes, loss brakes and every report read it, while a few messages read the
  settings: Telegram kept showing the old ~$99 although the settings said $5,000, and positions stayed under the $10
  minimum order. Now the desk applies the new budget at its next start like a deposit or a withdrawal: the
  difference goes to the cash, the trades, the result so far and the strategy history are kept, the day's loss limit
  moves with it, and Telegram confirms it (💰 Budget changed from ... to ...). The drawdown is measured from the
  change, so a withdrawal is not reported as a loss.
- On the Binance demo account the new budget is applied only if the demo wallet holds enough USDT (Binance refuses
  orders the wallet cannot pay for). Otherwise Telegram and `/health` say so and how to get the budget: paper
  trading, or a budget the wallet can cover. `/health` shows the budget the desk really trades with.
- The setup's "Start a fresh journal?" question explains what "n" does.

## 1.6.1 - 2026-10-25

Fixes from an independent review of 1.6.0 (26 confirmed findings, none affecting a desk without a free AI key):
- Google's "daily quota used up" reply was read as "slow down for a minute", because the deciding part comes after a
  long help text and the desk read only the first 800 characters. The whole reply is now read; "come back tomorrow"
  is recognised by whole words only, so a per-minute refusal is no longer mistaken for a daily one.
- Groq's daily token cap now counts in the plan, so the free AI is not spent by mid-morning.
- A "daily allowance used up" alert also when the desk's own counters run out first (the usual case), once a day.
- After a timeout or an outage the provider rests 10 minutes instead of 1, so a check pays at most one timeout per
  model; a lost connection rests all its models.
- A model that keeps refusing questions (HTTP 400) is switched off with an alert; a model the provider no longer
  accepts is reported once per model with the provider's own words; the JSON-form retry happens only when it can help.
- A key with an invisible or curly character from a copy-paste is reported clearly instead of failing silently.
- A request in flight across the provider's midnight no longer crashes a question; any AI error falls through to the
  next AI and then the rules.
- Privacy: Claude's review of the private notebook is left out of the playbook a free AI reads, and the lab's
  strategy proposals (built from the notebook's lessons and history) stay with Claude.
- The head trader lowers a threshold on strong news only for a reader with its own measured hit rate, and keeps
  Claude's news reserve for the refreshes the free AI cannot cover.
- `/ai default`; a later AI_ORDER change in desk setup wins over an earlier `/ai` choice; the notebook shows the order
  in force. Re-running desk setup keeps a saved free AI when its test question fails, and asks for model names.
- Texts: "no daily cap" instead of "$0.00"; the Claude-budget alert says the free AI takes over; scout picks made from
  Claude's web research are no longer labelled "market data only"; `.env.example` comments no longer read as values.

## 1.6.0 - 2026-10-25

- A free AI next to Claude, for small budgets. `desk setup` has a new step (STEP 3) to add one: Google Gemini
  (recommended: no card, about 500 questions a day per model), Groq, Mistral, Cloudflare Workers AI, OpenRouter or
  any OpenAI-compatible service. The free AI reads the news, double-checks each buy or sell the rules want and picks
  the scout's coins. Web research and `/review` of the private notebook stay with Claude.
- Order: the free AI first and Claude only when it cannot answer (default), or Claude first while its budget lasts.
  `/ai` on Telegram shows both AIs and the free AI's questions used today; `/ai free-first` or `/ai claude-first`
  changes the order at once and keeps it across restarts.
- The free AI is rationed by requests instead of dollars: every request is counted (failed ones too), 10% of each
  daily allowance is kept in reserve, per-minute limits are respected by a short wait or the next model, and nothing
  is retried in a loop. The head trader plans the buy/sell questions from the requests left before the provider's
  daily reset. Answers are checked against the question's form before any agent uses them.
- The scorecard judges each AI separately against the rules, and the news hit rate is shown per reader. Health,
  `/status`, `/aibudget`, `/diagnose`, the notebook and the daily report show the free AI. A rejected key, a
  retired model or a used-up daily allowance is reported once on Telegram. `desk check` tests the free AI.
- A free model may slow the check pace down but not speed it up (faster checks would spend its allowance).
- Fix: the desk assumed a Claude buy/sell question costs about four times what it does (6,000 input tokens instead
  of the measured 1,300), so at the start of each day it rationed Claude more tightly than its budget required.

## 1.5.0 - 2026-10-25

- Claude's budget no longer shrinks with a small demo balance. Until now it was the lower of $5 a day and 0.25% of
  the starting balance a month: on a $100 demo that is one cent a day, so Claude was switched off after one call
  and the free rules decided everything. On demo money there is now no percentage cap: the flat
  `AI_DAILY_BUDGET_USD` ($5 a day of real money) applies. `AI_BUDGET_PCT_MONTH` still works if you set it.
- `/aibudget` on Telegram: Claude's cap, today's spend per agent (news, decisions, coin scout, research, reviews)
  and the month at today's pace. `/aibudget 3` sets $3 a day at once and keeps it across restarts (at most $20 a
  day, so a typo cannot run up a bill); `/aibudget default` goes back to the settings file. Each change is logged.
- Fix: the trailing stop followed the live price inside the hour, while the backtest and the laboratory measured
  it on finished hourly closes. A brief spike raised the stop and the next ordinary dip sold the position: on the
  last 90 days of real prices the rules made +0.24% that way against +2.22% as measured. The live desk now raises
  the stop on finished hourly closes only; the stop order on the exchange still protects against any drop.
- Fix: keyword news (used when Claude is off or out of budget) counted the same story once per feed. Three titles
  of one "Ethereum delays Glamsterdam" story gave ETH the worst possible score (-1.0) and blocked every ETH buy.
  A story now counts once (titles sharing three significant words), words must match whole ("Again" is no
  longer "gain", "bank" is not "ban"), and a keyword score never blocks a buy on its own: only Claude's reading of
  the news can (it still counts in the score with its 10% weight).

## 1.4.6 - 2026-10-25

- Fix: the server rejected 1.4.4 and 1.4.5 because a test failed there and not elsewhere. The update job loads the
  server's `.env` into its environment, and the tests inherited it, so a test saw the real small balance and the
  new 1.4.5 warnings. The 1.3.3 fix had only hidden the file. Now every test runs with none of the settings
  variables set, and the update job runs the library install and the tests without the server's settings and
  API keys in their environment. Rehearsed with the 1.4.3 update job against a copy of a server with a small
  balance: 1.4.5 fails exactly as reported, 1.4.6 passes.

## 1.4.5 - 2026-10-25

- A small starting balance is now reported at start, in `/health` and in `/diagnose`, with its consequences:
  positions next to the $10 minimum order (a small loss stops all buying; at $100 the desk stops after its first
  loss), and a Claude budget of a cent a day (it is 0.25% of the balance a month), which switches Claude off.
- The "Claude's daily budget is used up" alert says where the cap comes from and what raises it.
- `/diagnose` shows the starting balance, equity, cash, position size, the Claude cap and, in demo mode, what the
  Binance demo wallet really holds.
- The setup wizard warns when the demo wallet holds under 1,000 USDT (it proposes 90% of it as the budget).

## 1.4.4 - 2026-10-25

- `/diagnose [days]`: "check the logs" from the phone. The settings in force, every coin's buy signals and what
  stopped each one (checklist rule, risk limit, Claude veto, bad news, the head trader's portfolio tilt), order
  failures, hours the desk was not checking, and the number of trades its own rules would have made on the same
  coins and real prices. When the live desk trades much less than its rules, the report names the largest causes.

## 1.4.3 - 2026-10-25

Fixes from an independent review of 1.4.0 (22 confirmed findings).

- Fix (live trading): the head trader's portfolio and news tilts were also applied to coins already held, where
  a higher threshold makes SELLING harder. With today's correlations (0.8-0.9 between all majors) every held coin's
  sell bar moved from -0.35 to -0.45 with three positions. Held coins now keep the strategy's own sell bar.
- Fix: a per-coin threshold card could never pass the exam, because the coin set it did not touch tied with the
  champion and ties counted as losses. The exam now judges only tests where the card trades differently, counts
  repeated identical windows once, and requires at least three such tests.
- The threshold study is rebuilt on the review's statistics: only the coins the exam trades; a coin needs a
  profitable threshold that beats the common one by a point and wins both half-years with 12+ trades in each;
  shrinkage works in whole steps (it was cancelled by rounding); a card changes at most two coins, each coin
  counting as a change; one study card per 90-day held-out period; the playbook calls a study pick a proposal
  under test, not a rule. On today's data no coin qualifies.
- Fix: 1.4.0 changed every stored strategy id, so `/approve` and `/reject` failed for challengers created
  before it. Ids from before 1.4.0 are valid again and ids stored since are repaired at start.
- The exam and the study run on the card's own rules instead of changing the live desk's rules while they run.
- The forward test keeps waiting (up to 90 days) when too few trades happened, instead of failing the card for a
  quiet market; it judges both exam coin sets.
- The weekly lab stops between tests when the desk restarts (and runs again afterwards) instead of being killed.
- Claude's budget: spend is tracked per agent (news, decisions, scout, research, review); the head trader keeps
  enough for the news agent's remaining refreshes, plans at the desk's actual check pace, and does not spend a
  call on a coin whose recent opinion still applies or whose buy the news veto refuses anyway.
- History downloads retry network errors and rate limits; `backtest --coin-buy` reports a typo instead of crashing.

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
