# Changelog

Release numbers follow `major.minor.patch`: a new **minor** number for new abilities, a new **patch** number for
fixes only, and a new **major** number only if the owner has to do something by hand (such as re-running the setup).
The number lives in `trader/__init__.py`; `/version` on Telegram and `desk version` on the server show it next to
the exact code revision, and every update message names the version it installed.

## 1.7.6 - 2026-10-27

- The last finding of the phone-setup review: a request file holding data nested thousands of levels deep (valid,
  but more than the reader can unpack) stopped the update job before it could report anything, at every run. Such a
  file is now treated like any unreadable request: refused, removed and reported.

## 1.7.5 - 2026-10-27

Fixes from a security and conversation review of the phone setup (`/setup`), each one reproduced first:
- **The update job no longer trusts what the desk writes.** It accepted values such as `15.0`, `1e1`, `$15` or
  `50,000` and wrote them as typed, but the desk cannot start with them, and the update job itself then stopped too
  (only a terminal session could fix it). Now only the exact form the desk's settings loader reads is accepted, coin
  lists cannot carry a hidden line break, and after writing the file the job checks in a fresh process that the desk
  can start with it; if not, it puts the previous file back and says so. If the settings file ever does not load
  (for example after a hand edit), the update job keeps working (so a fixed version can still be installed) and
  tells you once on Telegram.
- The copy of the previous settings (`data/.env.previous`, which holds the keys) and the update job's status and
  restart files are now replaced, never written through a link someone placed in the data folder, so nothing in that
  folder can make the job copy the keys into, or overwrite, another file. A request that is not a plain file is
  refused at once instead of making the job wait.
- Settings sent from the phone are applied only after an interrupted update is finished or undone, so a restart for
  new settings never starts code whose tests did not finish.
- `/update` and `/setup` sent close together no longer cancel each other, a request written while the job is busy is
  kept for its next run (it was deleted unanswered), and a second `/setup` keeps the first one's changes while they
  wait. A request that reaches the job more than an hour late is not applied (you are told).
- Phone answers: a budget of `10,000` meant $10 and `1234567` was saved rounded; budgets are now read the way people
  write them. Re-entering the current check interval or report hour is no longer offered as a change. Choosing the
  trend rule and then the hourly agents again (or the reverse) in one session now leaves your saved coins, slots and
  scout alone. The trend coins question takes only its answers (0 goes back). A very long coin message is refused
  before any Binance lookup, and the lookups stop at the first unknown coin. `Trend` or `Testnet` typed by hand in the
  settings file is read as the desk reads it. A phone change no longer fills a missing trading mode with the demo
  account or turns an emptied checklist back on.
- Before you confirm, the setup names the coins the trend rule would sell because they are not in the new list. The
  trading mode is not changed from the phone while coins are held, and the demo account's keys are checked first.
- A message `*` with no setup open gets the help again.

Fixes to the daily trend rule from the same review:
- An hourly position whose trend sale failed (exchange busy, no price) was "adopted" in the same step: its stop was
  loosened and you were told it now follows the rule. It now keeps its exits until the next try sells it.
- In paper trading, a simulated stop is recorded at the minute it was hit (the re-entry rule counts from it), and parts
  of a protective order that sold on the demo exchange are recorded at their sale time.
- Sale and hourly buy messages keep the digits of cheap coins' prices.
- Texts: the start message says "simulated stop orders" in paper trading, the "incomplete after N tries" lines give
  the real number of tries, a sale with no live price is shown as a sale, and a held coin without enough daily history
  says so instead of "still above its average".

## 1.7.4 - 2026-10-27

New: **the setup on your phone.** `/setup` on Telegram changes the strategy, coins, risk level, budget, trading mode,
check interval, daily report hour and pulse with buttons. The desk cannot write its own settings file (the server's
sandbox forbids it, on purpose), so it hands the change to the update job, which checks every value again, keeps the
previous file as `data/.env.previous`, writes the new one, restarts the desk and confirms on Telegram. Keys and tokens
are never asked for on Telegram; they stay a `desk setup` job on the server. A settings change never installs code by
itself.

Fixes from independent reviews of the daily trend strategy:
- A slow memory leak: after an emergency stop the desk kept a new copy of a coin's daily prices every day (hundreds
  of MB in a year on the 1 GB server); it now keeps one per coin.
- A sale the exchange refused was shown in the daily summary as "holds: still above its average"; it now says the
  sale failed and the coin is still held. A day's summary keeps the buys and sales of earlier tries, and a summary
  that could not be sent (Telegram unreachable) is sent at the next check.
- The re-entry rule after a stop counts from the time Binance filled the stop, not from when the desk noticed it at its
  next check (a stop at 23:50 noticed at 00:05 belonged to the wrong day). Exchange exits are recorded at their fill
  time for both strategies.
- `/diagnose` judges what the rule would hold today with the same loss brake and cash limits as the desk.
- The "a rule refused every buy" alarm no longer fires in trend mode for the desk's designed pauses (the loss brakes,
  full slots), and the "decisions late" alert is not repeated for a day already reported as given up.
- `TREND_SMA_DAYS=nan` or `inf` no longer stops the desk.
- After a switch back to the hourly agents, the weekly lab's lessons leave out only the trend rule's own period and
  trades (they had left out everything before the switch).
- In replays, holding a coin listed during the period now counts from its listing, and a request for N days returns N
  finished days.
- Texts: `/strategies` says where the emergency stop lives in paper trading and how full slots work; `/aibudget` no
  longer projects a monthly cost from one `/review`; `desk check` tells a coin whose prices could not be read from one
  with too little history, flags coins without a stop-market order for the trend rule, and treats a busy free AI as
  information in trend mode; prices of cheap coins keep their digits.

## 1.7.3 - 2026-10-27

Your desk keeps trading exactly as before (the hourly agents) until you choose the daily trend rule in `desk setup`.
With the daily trend rule:
- A short summary of each day's decisions arrives on Telegram after 00:00 UTC, silently (no sound at night): what was
  bought and sold, the coins held with how far they are above their average, coins waiting after a stop, refused buys
  and the slots used. Messages also say when a winner's take-profit order is raised and when a position bought by the
  hourly strategy is taken over (with its new emergency stop).
- `/status`, `/watchlist`, `/strategies`, `/ai`, `/aibudget`, the help, the start message and the daily report
  describe the trend desk; `/approve` and `/reject` explain that strategy cards wait for the hourly strategy;
  `desk check` tests the daily history the rule needs instead of the news and forex feeds it does not use.
- An alert when the day's decisions have not run by 02:00 UTC, and the stuck-rule alarm judges the rule over its own
  last 7 days.
- `/review` tells Claude that the desk runs the fixed trend rule; after a switch back to the hourly agents, the weekly
  lab's lessons leave out the trend rule's trades.
- The notebook no longer says the emergency stop is "on the exchange" in paper trading, and shows a refused buy as
  "BUY refused".
- (Correction: the line saying when a day's daily prices were incomplete is in `/agents`' Market data row, since 1.7.2.)

## 1.7.2 - 2026-10-27

Your desk keeps trading exactly as before (the hourly agents) until you choose the daily trend rule in `desk setup`.
- A typo in a trend setting (`TREND_STOP_PCT=0,25`, `20%`, an empty value or a word) no longer stops the desk and its
  updates: a decimal comma and a % sign are understood, anything else falls back to the tested value with a message.
  `/health` names the setting that is out of range.
- `desk setup`: a saved `STRATEGY=Trend` (any capitals) is kept when you press Enter; a risk level you set by hand is
  kept; going back to the hourly agents proposes the coin scout again; the Claude and free-AI steps say that the daily
  trend rule needs neither.
- `/diagnose` compares the desk with what the rule holds after its latest daily decision (it was one day behind and
  reported differences that were not there).
- After an emergency stop older than about ten weeks, the re-entry rule now reads enough days to see whether the
  coin's trend has reset (it could buy again too early with a stop much tighter than the tested 25%).
- The 1.7.1 warning for hourly desks with more than 3 positions is removed: the hourly strategy simply trades within
  its tested limits (3 positions, 10% each), as the README says.
- The guide and README state the risk limits of each strategy as they really work: the trend rule can have up to 80% of
  the account in coins (one 10% slot per coin), its daily-loss limit practically never acts (it buys only at the start
  of the day), and the 10% overall brake will usually trip in a bad stretch; how to replay the rule
  (`backtest --strategy trend`, `/trendtest`), the setup questions in order, and that AI keys are optional for it.

## 1.7.1 - 2026-10-27

The daily trend strategy, finished and made safer (independent reviews of 1.7.0 by subsystem):
- Orders: a coin whose live price could not be read no longer stops the day's decisions at every check; a take-profit
  raise that Binance refuses puts the previous protective pair back (the coin is never left without its stop), and is
  announced only when it really happened; a position taken over from the hourly strategy is marked as taken over only
  once its new stop is really in place; the take-profit placed is what Binance accepts (at most twice its recent
  average price); a coin that offers only stop-limit orders gets a clear message instead of a refused order at every
  check (the desk watches its stop itself); between checks the desk sells a trend position only at its emergency stop,
  never at the take-profit placeholder; a sale filled in part is tried again; the daily step is retried for at least
  an hour.
- `/health`, `/agents`, the pulse and `/diagnose` describe the trend desk: the date of the last daily decision (with
  a warning when it has not run or gave up), the agents that work in trend mode, and `/diagnose` compares what the
  desk holds with what the rule, replayed on the same days, would hold. The notebook (STRATEGY, README, TRADES,
  PERFORMANCE and the digest `/review` reads) is written for the trend rule. The remaining Telegram commands and the
  daily summary of the trend decisions follow in the next release.
- The weekly lab can no longer promote, roll back or record a strategy card while the trend rule trades (`/approve`
  no longer fails in trend mode, it finds nothing to approve), nor judge an
  hourly card on the trend rule's trades after a switch back (its record restarts at the switch, its lessons leave out
  the trend trades, and challengers approved before the trend period are tested again).
- Both strategies: paper trading no longer fills a stop that was moved above the price at that higher price (Binance
  refuses such an order; the desk now sells at the market price), so paper results in fast falls were slightly too
  good; a stop-loss that filled in part and then expired on the thin demo book is recorded as a stop-loss at once.
- Settings: an hourly desk with more than 3 positions or more than 10% per position is told that the hourly strategy
  trades within those limits.

## 1.7.0 - 2026-10-27

New: **the daily trend strategy** (`STRATEGY=trend`, chosen in `desk setup` step 5; README "The daily trend
strategy"). Every replay of the hourly agents lost money after fees; this one plain rule held up in every test:
- Hold a coin while its finished daily close is above its 50-day average, sell after a close below; decide once a day
  at the first check after 00:00 UTC. Each coin's share is 1 / number of coins, smaller for coins that swing more than
  50% a year, at most `MAX_POSITION_PCT`; when slots are short, the strongest 60-day gain goes first.
- A 25% emergency stop on Binance for every position; after it fires the coin is bought again only once its trend has
  reset. No profit target: Binance's protective pair needs a take-profit price below twice the market, so the desk
  keeps it at 1.9x the price and moves it up as the price rises.
- No AI in the decisions, no coin scout, no head trader, no weekly lab (they belong to the hourly strategy and stay
  available for `STRATEGY=hourly`). Every risk limit stays: position slots, the daily-loss limit, the 10% overall
  brake.
- Switching keeps the record: coins outside `SYMBOLS` are sold at the first daily decision, coins in the list follow
  the rule (their stop becomes the emergency stop). The trend goals count from the day the rule started, on the
  trades it opened itself: 26 weeks, 30 trades, profit factor 1.5, worst drop at most 30%.
- Replays on real prices with the desk's limits (8 coins): +192% from Oct 2023 to Apr 2026 while holding made +127%;
  -22% in the 2022 crash while holding lost 76%; -4% in the last 365 days while holding lost 47%; +19% in the last
  180 days while holding made +3%. Worst drops 28-40%; the "Calmer" risk level (`TREND_VOL_TARGET=0.25`) roughly
  halves them.
- `/trendtest [days]` on Telegram replays the rule on your coins (also before you switch); `desk backtest --strategy
  trend` on the server (for servers installed before 1.7.0: `.venv/bin/python -m trader backtest --strategy trend`).
- On the Binance demo exchange a coin that is missing or priced more than 3% from the real market is not bought (the
  desk values its coins at real prices); `desk setup` warns about such coins.
- Buy and sell messages, `/status`, `/report`, `/diagnose`'s replay and the settings lines describe the trend rule.
  The next release finishes the rest (`/health`, `/agents`, `/strategies`, the notebook, the help, and a short daily
  summary of the trend decisions on Telegram).
- Also for both strategies: the Risk Manager's limits are shared by every kind of buy; nothing changes for the hourly
  strategy.

## 1.6.7 - 2026-10-27

Fixes found by an independent review of 1.6.6:
- A stop-loss or take-profit that filled completely between two checks was recorded as "sold on the exchange" instead
  of "stop-loss (exchange)" / "take-profit (exchange)", because the new holdings check found the sale first. Money and
  results were right, but the desk's "no re-buy within 24 hours after a stop-loss" check did not see the stop (it
  could buy the coin straight back), and the reports' exit counts and price-gap measurement left these exits out.
  Sales of the position's own protective orders are now recorded under their real reason and at the price they were
  set at.
- A part-filled protective order whose cancel got no answer from Binance (a timeout) was forgotten although its rest
  still held the coins: the coins stayed locked on the exchange with no stop-loss. The desk now keeps the order on
  record and tries again at the next check.
- A last sale of exactly one step (the smallest amount the exchange trades) was skipped as rounding noise, leaving a
  coin in the journal that the exchange no longer held.
- A sale by an order placed before the position's own buy (for example a protective order an earlier record left
  live after `desk reset --force`) could be counted as this position's sale; such orders are now left out.
- Prices of these recorded sales are the price before fees, as for every other order, so the price-gap figure no
  longer shows a 0.1% gap that did not happen.

## 1.6.6 - 2026-10-27

- Fix: a protective order (stop-loss / take-profit pair) that filled only in part on the thin demo order book left
  the sold part unrecorded when the desk later cancelled or replaced the order (to move the trailing stop, or to
  sell), or while the order stayed part-filled. The journal then kept coins that no longer existed (the owner's
  case: 71.6 UNI recorded, 61 on the exchange), missed the money of that sale, and could end with a position that
  could never be sold, blocking a slot; the warning wrongly advised a full `desk reset`. The desk now reads the
  account's sales of the coin since the position opened and records any part sold that it has not recorded yet:
  in the holdings check, when it finds a part-filled order (it then cancels the rest and protects the remaining
  coins again with a fresh pair), after cancelling an order, and when the exchange reports an exit (only the part not
  recorded yet). Only a true demo reset (no coins left and no sale found) still suggests `desk reset`.

## 1.6.5 - 2026-10-27

- `/health` says how long a market check takes on this server ("Server speed: a check takes about 41 s (slowest of the
  last 96: 2.3 min); plenty of time to spare"), with a warning when the typical check takes more than 5 minutes. It
  answers "is the server too slow?" from the desk's own measurements.

## 1.6.4 - 2026-10-26

- The demo goals say when the 150-trade sample will be complete at the pace so far ("20 of 150 trades needed (at 10 a
  week, complete in about week 15)"). A replay of three years of real prices showed that this strategy closes 4 to 11
  trades a week, so the sample takes about 4 to 7 months, not 12 weeks; the goals stay as they are, and the line now
  says so instead of looking like a failure in the first weeks.

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
