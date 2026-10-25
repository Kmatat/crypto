# Crypto Multi-Agent Trading Desk (Demo Phase)

A team of software "agents" that reads news, forex and price data, analyses it, makes a
forecast, decides whether to buy or sell, and **places real orders through the Binance API
on a demo account (Binance Spot Testnet, which uses fake money)**. Every decision and trade is
recorded so you can judge performance before any real money is involved.

> **Real-money trading is turned off on purpose.** The only allowed modes are `simulation`
> and `testnet`. If `TRADING_MODE` is set to anything else, the program refuses to start.

**Quickest start: run it 24/7 on a free server, with alerts on your phone.**
Follow the click-by-click guide in [deploy/FREE_SERVER_GUIDE.md](deploy/FREE_SERVER_GUIDE.md).

---

## How the desk works

Think of it as a small trading firm where each employee has one job:

| Agent | Role in business terms | What it actually does |
|---|---|---|
| **Market Scout** | Finds new opportunities | Every 6 hours scans all ~770 Binance USDT coins and keeps the liquid crypto ones; stablecoins, gold tokens and tokenized stocks are filtered out. Claude then **searches the web** for today's news and catalysts and picks up to 8 coins to watch, each with a reason. Without Claude it adds the most-traded established coins |
| **Market Data** | The data clerk | Pulls hourly price candles for each watched coin from Binance |
| **News Analyst** | Reads the headlines, sets the pace | Reads CoinDesk, Cointelegraph and Decrypt. Claude rates each coin's news from -1 (bearish) to +1 (bullish) and advises how often to check the market right now. With no Claude key it uses a keyword list |
| **Forex / Macro** | Watches the US dollar | Follows USD vs EUR, GBP, JPY and CHF (European Central Bank rates). A fast-rising dollar usually hurts crypto ("risk-off") |
| **Technical Analyst** | Chart reader | Standard indicators: trend averages (EMA), momentum (MACD), overbought/oversold (RSI), Bollinger bands, volatility (ATR) |
| **Forecaster** | Predicts the next move | Fits a trend line to the last 24 hours, compares it with the noise, and gives a probability that the price goes up |
| **Portfolio Manager** | Makes the call | Weighs all reports and decides **BUY / SELL / HOLD**, with a confidence level and a plain-English reason. Uses Claude if a key is set, otherwise a weighted score |
| **Mentor** | The master traders' checklist | Every candidate buy must pass rules documented by well-known traders: above the 200-day average (Paul Tudor Jones), Bitcoin itself in an uptrend (don't fight the tape), no re-entry 24 h after a stop-loss (Livermore), at least 2:1 reward-to-risk (PTJ). Each line is shown with its source in the buy alert, and Claude is told to weigh it |
| **Risk Manager** | Safety officer, can veto anything | Sizes each trade so a stop-loss costs 1% of equity (the Turtle Traders' method), capped at 15% per coin; sets the stop-loss and a 2:1 take-profit; **trails the stop upward** as the price rises (Seykota: cut losses, let profits run). Blocks trades with low confidence, too many open positions, or after the 3% daily / 10% overall loss limits |
| **Execution Trader** | Places the order, then insures it | Sends the order to Binance, confirms it was **FILLED**, records the real quantity, price and fees, and immediately places the **stop-loss and take-profit on the exchange** as a pair (when one fills, Binance cancels the other) |

Every 15 minutes (the AI may adjust this between 5 and 60), for each watched coin:

```
(every 6 h) Market Scout: refresh the coin list from a market scan + web research
               ▼
prices ──► did the stop-loss / take-profit order on Binance fire? ──yes──► record the exit
               │ no
               ▼
   Technical + Forecast + News + Forex reports
               ▼
   Portfolio Manager: BUY / SELL / HOLD + confidence
               ▼
   Risk Manager: approve or veto, set size and exits (strongest buy signals get the free slots first)
               ▼
   Execution: place order ► confirm FILLED ► record in journal
              ► place stop-loss + take-profit pair on Binance (OCO)
               ▼
   Pace: next check in 15 min, or what the AI advises (5-60 min);
         5 min whenever an UNPROTECTED position is close to its stop-loss or target
```

**Why the stop-loss lives on the exchange.** Between two checks the desk is blind. If a coin
crashes 8% in ten minutes, a desk-side stop would only react at the next check, well past the
intended exit. With the orders on Binance, the exchange itself sells within seconds of the price
touching the stop, 24/7, even if the server is down. The pair is one-cancels-the-other (OCO): when
the stop-loss fires the take-profit is cancelled, and vice versa. The desk keeps a backup: positions
whose exchange orders could not be placed (for example because the price was already past the level,
or the testnet rejected them) are checked by the desk every 5 minutes and re-protected on each try.
In paper trading, the same protection is **emulated** from 1-minute price data, so simulation results
stay comparable with the demo account.

**Checking more often doesn't mean trading on noisier charts.** The agents still judge trends on
hourly candles. Checking every 15 minutes means they react sooner to news, to stop-losses and to new
signals.

Default weights for the rule-based decision: technical 50%, forecast 30%, news 10%, forex 10%. News and
forex weights are small because their accuracy is unmeasured; clearly negative news (score ≤ -0.5) still
blocks a new buy.

---

## The three ways to run it

| Mode | Needs | Prices | Orders | Purpose |
|---|---|---|---|---|
| `run --offline` | nothing | Made-up (random walk) | Simulated locally | Check that the machinery works. **The profit numbers mean nothing** |
| `TRADING_MODE=simulation` | internet | Real Binance prices | Simulated locally (paper trading) | Test the strategy on real markets with no account. Works from any country |
| `TRADING_MODE=testnet` | free Binance Testnet keys | Real Binance prices for analysis | **Real API orders on the testnet, fake money** | Full rehearsal: the same API calls a live account would use. Needs a country Binance supports |

## Setup (about 10 minutes)

1. Install Python 3.10 or newer, then run:
   ```bash
   pip install -r requirements.txt
   python -m trader setup
   ```
2. The setup asks a few questions and checks each key right away: trading mode, Binance demo
   keys (from <https://testnet.binance.vision> → *Generate HMAC_SHA256 Key*), Claude (optional, paid), a free AI
   (optional, no cost) and Telegram alerts (optional). It saves everything in `.env`. Run it again any time to change
   something.
3. *(Optional, recommended)* add an AI so it reads the news and double-checks each decision with a written reason:
   a free AI (Google Gemini, no card), Claude (paid, best judgement and web research), or both.
4. Check that every connection works:
   ```bash
   python -m trader check
   ```
5. Run it:
   ```bash
   python -m trader run --once     # one cycle, to watch what it does
   python -m trader run            # keep running (one cycle per CYCLE_MINUTES)
   python -m trader report         # performance report at any time
   ```

Try it with no setup at all: `python -m trader run --offline --cycles 300 --fast`

## Updates without a terminal

The server keeps itself current. A small update job runs every 30 minutes (and immediately when you
send `/update` on Telegram):

1. Looks for new code on GitHub. Nothing new → nothing happens.
2. Downloads it, installs libraries, and **runs the automated test suite first**.
3. Only if the tests pass does it restart the desk, which finishes its current market check before
   stopping, so an update can never interrupt an order halfway.
4. Confirms the new version is running. If the tests fail or it crashes on start, the previous version
   is **restored automatically**.
5. Tells you the outcome on Telegram, with a one-line summary of what changed.

Every release has a number (`CHANGELOG.md`, declared in `trader/__init__.py`); update messages and `/version`
name it next to the exact code revision, for example *"Updated to version 1.3.0 (02923ca)"*.
A server installed before this job existed needs one terminal session to join in: see "One-time upgrade" in
`deploy/FREE_SERVER_GUIDE.md`. `/version` shows what is running and whether a newer version exists. Set `AUTO_UPDATE=false` in `.env`
to be notified about new versions instead of installing them automatically. Anyone who can push code to
the GitHub repository can change what the server runs, so protect that account with two-factor
authentication (see the security checklist in the server guide).

## Measuring the strategy before trusting it

`python -m trader backtest --days 180` replays the desk's rule-based decision logic (indicators,
forecast, weighted score, the master traders' checklist, sizing, trailing stops, risk limits, fees,
exchange-side exits) over real hourly Binance history and prints return, trade count, win rate, profit
factor and drawdown next to the buy-and-hold result for the same coins and period. `--sweep` tries a grid
of thresholds and stop/take-profit distances; `--checks`, `--trail`, `--risk`, `--position`, `--symbols`
test the principles, sizing and coin sets. News, forex and Claude's judgement have no history and are left
out, so the backtest measures the free rule core: the floor the AI layer has to beat.

What the backtests showed when the defaults were chosen (BTC, ETH, SOL; buy-and-hold of the same coins in
brackets):

| Setting | 90 days | 180 days | 365 days |
|---|---|---|---|
| Plain indicator rules (the original) | -2.2% (+26%) | -3.1% (+10%) | -10.1% (-38%) |
| + 200-day rule and Bitcoin regime only | -0.3% | -0.3% | -0.9% |
| **Current defaults** (checklist, 2:1 target, trailing stop, 1% risk sizing, 10% cap, news weight 10%) | **+1.9%** | **+1.9%** | **+1.3%** |

The laboratory's own exam is stricter than this table and worth knowing: across its 10 tests (the same
three windows plus two that ended 3 and 6 months ago, each on BTC/ETH/SOL and on BNB/XRP/ADA) the current
defaults average only **+0.05%** with a profit factor of 0.79. The recent windows on the major coins are the
strongest; older periods and the second coin set are roughly break-even. Read the table as "best recent case".

The original rules had no edge: they lost a little in rallies and more in the bear market, and exchange
fees were the whole difference (with fees set to zero the same trades made +5.7% over 180 days; 11 trades a
week on $10,000 costs about 11% of the account per year). The checklist keeps the desk out of downtrends,
which is where most of the loss came from, cuts the number of trades by two thirds, and the exit discipline
makes average winners about twice the size of average losers. The edge is modest, it lags buy-and-hold in
strong rallies (the desk is mostly in cash), and it was negative on one of three alternative coin sets
tested (BNB, XRP, DOGE). About 100 trades per year is too few to be statistically sure of anything: treat
these numbers as "plausibly positive", not proven. A later statistical check of a 365-day run made the point
sharper: 102 trades with an average gain of $1.56 and a spread of $22 per trade (a t-statistic of 0.7) is
indistinguishable from luck; the five best trades, all in SOL, exceeded the whole year's profit, and the desk sat
in cash for eight of the twelve months. Expect roughly zero until the laboratory finds something better.
Since 1.3.1 the live desk decides on finished candles only, as the backtester does, so these measurements
describe the running desk; the still-forming candle supplies only the live price for stops and sizing. Independent reviews also found that 4-hour candles
(`CANDLE_INTERVAL=4h`, `--interval 4h`) and holding at most one or two positions reduce fees further; with
the checklist in place they changed results by less than a point either way, so the defaults stay at
1-hour candles and 3 positions. Any change to the strategy should show an improvement here on at least two
different periods and two coin sets before it is trusted, and even then live results will differ.

**Known limits of the risk controls (from the same reviews):** at 10% per position with a 2 ATR stop, a
losing trade costs about 0.15% of the account, so the 3% daily limit almost never triggers (it remains a
backstop for flash crashes). The 10% overall brake is the control that matters: in the past year's prices it
would have tripped and saved 6-16 points. When it trips, the desk now pauses new buys for 14 days
(`BRAKE_PAUSE_DAYS`), then resumes at half size until the account has recovered half the loss.

## How the desk improves itself (the strategy laboratory)

The desk can create, test, promote and roll back strategies on its own, but inside a frame designed to
keep it honest, because the reviews showed that on these markets random entries do about as well as
indicators, so a loop that "keeps whatever backtested best" would mostly keep luck.

```
 Research agent ──► strategy cards (proposals)
 (web research with Claude, or         │
  neighbours of the champion)          ▼
                              Laboratory exam: 5 periods × 2 coin sets, must beat the champion on
                              most of them, never badly worse, enough trades, drawdown within limits
                                       │ pass
                                       ▼
                              Forward test: the weeks AFTER the proposal (data that did not exist yet)
                                       │ pass
                                       ▼
                              Owner approves on Telegram (/approve <id>)  ──►  new champion
                                       │                                        │
                              AUTO_PROMOTE=true skips this step         live watch: 30+ trades with a
                                                                        profit factor under 0.7 → rollback
```

- **A strategy card** is a complete configuration: entry threshold, stop, target, trailing distance, which
  checklist rules, cooling-off, reward-to-risk, sizing, position count and candle size. It is data, so the
  system can invent as many as it likes. New *kinds* of rules still need code, which arrives through the
  tested update pipeline; the research agent lists such ideas for the developer instead of inventing code.
- **Hard bounds** (`trader/strategy.py`): a card may never enlarge positions beyond the owner's limits, never
  touch the daily/overall loss brakes, the AI budget, the trading mode or the coin filters, and may change at
  most two parameters at once versus the champion.
- **Weekly run** (Monday, with the daily report): post-mortem of the last 30 days' losing trades ("lessons"),
  a few proposals, the exam, forward tests of earlier proposals, and a Telegram summary. `/strategies` shows
  the champion, the challengers and their status; `/lab` runs it now; `/approve <id>` and `/reject <id>` decide.
- **Rollback:** a promoted champion whose live trades (30 or more) show a profit factor under 0.7 is replaced
  by the previous champion automatically, with a Telegram alert. Every promotion and rollback is logged.
- **Claude is measured, not trusted:** it is now a pure veto (consulted only when the rules would trade), it
  is not consulted on buys the checklist or the risk limits would refuse anyway, and a code-computed
  **scorecard** replays every veto against the real prices that followed: money the vetoes saved, money they
  cost, and the hit rate of the news scores. The daily report shows it; after 50 divergent decisions it gives
  a verdict. The AI budget is a flat daily cap in real dollars (`AI_DAILY_BUDGET_USD`, $5): send `/aibudget` to see
  today's spend per agent and `/aibudget 3` to change it from the phone (at most $20 a day; `/aibudget default`
  goes back to the settings file). An extra cap as a percentage of the account (`AI_BUDGET_PCT_MONTH`) is
  optional and off on demo money, because a small demo balance would starve Claude (0.25% of $100 a month is one
  cent a day).

## A free AI next to Claude (small budgets)

Claude is paid in real dollars even when the trading money is fake. A **free AI** answers the everyday questions at
no cost, within the provider's daily allowance: it reads the news, double-checks each buy or sell the rules want
(it can veto, never originate a trade) and picks coins for the scout. One client (`trader/free_ai.py`) talks to any
service with an OpenAI-compatible chat API; presets cover the free tiers checked on 2026-10-25:

| `FREE_AI_PROVIDER` | Free allowance (planned below the published limit) | Notes |
|---|---|---|
| `gemini` (recommended) | `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`, about 400 questions a day each, 8 a minute | No card. The free tier may use prompts to improve Google's models (not in the EEA, UK or Switzerland); daily quota restarts at midnight US Pacific |
| `groq` | `openai/gpt-oss-120b` and `-20b`, about 900 a day each, but only 8,000 tokens a minute and 200,000 a day (a token is about three quarters of a word) | No card; does not keep or train on prompts |
| `mistral` | `mistral-small-latest`, `ministral-8b-latest` | Free mode; turn off "Anonymous improvement data" |
| `cloudflare` | Gemma 4 on Workers AI, about 180 a day (10,000 "neurons") | Needs the account id in `FREE_AI_BASE_URL`; does not train |
| `openrouter` | `openrouter/free`, 50 a day: fewer than the news alone needs; 1,000 after a one-time $10 purchase (then set `FREE_AI_DAILY_REQUESTS=900`) | |
| `custom` | any OpenAI-compatible address (`FREE_AI_BASE_URL`, `FREE_AI_MODELS`) | e.g. a local Ollama server |

The desk needs about 90 questions on a normal day (measured: news every 30 minutes, about 35 buy/sell checks, the
scout every 6 hours) and up to about 320 on a very busy one, so Gemini's two models carry it with room to spare.

How it behaves:
- **Order** (`AI_ORDER`, or `/ai` on Telegram): `free-first` (default) asks the free AI and pays Claude only when the
  free AI cannot answer; `claude-first` uses Claude while its daily budget lasts, then the free AI. The free rules
  come after both.
- **Always Claude:** web research (the scout's and the weekly lab's), the lab's strategy proposals (built from the
  notebook's lessons and strategy history) and `/review` of the private notebook. A free AI sees public headlines,
  market numbers and the desk's strategy rules, never balances, keys or the notebook (Claude's review of it is left
  out of the playbook it reads).
- **Rationed by requests, not dollars:** the desk counts every request (failed ones too, as Google does), keeps 10%
  of each daily allowance in reserve (requests, and tokens where the provider also caps tokens a day), waits briefly
  for a per-minute slot or moves to the next model, and never retries in a loop. After a timeout or an outage the
  provider rests 10 minutes, so a market check pays at most one timeout per model. The head trader spreads the
  remaining questions over the checks left before the provider's daily reset, after keeping what the news agent and
  the scout will need.
- **Checked answers:** every answer must match the question's form (JSON schema) before an agent sees it; a wrong one
  goes to the next model, then Claude, then the rules.
- **Measured separately:** decisions carry their source (`claude`, `ai:gemini`, `rules`); the scorecard judges each AI
  against the rules on real later prices, and the news hit rate is shown per reader. A free model may slow the
  check pace down but not speed it up.
- **Problems are reported once on Telegram:** a rejected key, a model the provider no longer accepts, the daily
  allowance used up (by the provider's count or the desk's own).
- **News tilt per reader:** the head trader lowers a threshold on strong news only for a reader whose own news scores
  have a measured hit rate (a new free AI does not inherit Claude's record).
- **The key** stays in `.env`, travels only in the request header and is never logged.

Free tiers change without notice (in 2026 several were cut or closed), so every model name and the daily
allowance are settings (`FREE_AI_MODELS`, `FREE_AI_DAILY_REQUESTS`), and the desk keeps working without any AI.

## Is it working? Health, agents, pulse and watchdog

`/health` answers "is everything working" in one message (version, last market check, errors, settings
problems, update state, and the **decision funnel**: of the buy signals the rules produced in the last 24
hours, how many were bought, refused by which rule, or vetoed by Claude). `/agents` lists the 14 agents with
what each did and the strategies created, examined, promoted, rejected or rolled back. A pulse every
`PULSE_HOURS` (6) says what the desk did, so a silent phone means a problem. A watchdog in the separate update
job alerts when the desk stops checking the market and restarts it if it stays stuck.

The funnel exists because of a real fault: settings files from before 1.3.0 pinned a 3 ATR take-profit, which
the checklist's 2:1 reward rule refuses every time, so the desk could not buy at all. 1.4.1 corrects that value
at start, refuses impossible settings and strategy cards, and raises an alert when one rule blocks every buy
signal for a day.

## The head trader, and thresholds that each coin has to earn

One buy threshold for every coin treats Bitcoin's signals and a small alt-coin's signals as equally
trustworthy. They are not. Since 1.4.0 a strategy card can carry a **threshold per coin** (sell is always the
mirror image), and a **head trader** agent manages the other agents around it:

- **The threshold study** (weekly, `trader/calibration.py`): each coin the exam trades (the core coins and the
  second coin set) is replayed at every allowed threshold over the year that ends 90 days ago, judged as two
  half-years. A coin gets its own threshold only if that threshold made money after fees, beat the common one by
  at least one point, won in BOTH halves with at least 12 trades in each (a pick that works in one market phase
  is not evidence), and is then moved back towards the common threshold in whole steps in proportion to the
  sample (one step needs more than 20 trades). A card changes at most two coins, and only one study card is
  examined per 90 days, so the exam's newest window is always new data. Then the exam (whose ties - tests the
  card does not change - count neither for nor against it), the forward test and your `/approve`. On today's
  data no coin qualifies: every coin had 0 trades in the second half of the study year, because the checklist
  kept the desk in cash through the down-trend, and the study says so.
- **Portfolio and news at decision time:** before buying, the head trader raises a coin's threshold by 0.05 for
  every coin already held that moves with it (correlation above 0.7: the same bet twice), and tilts it for news
  only once the scorecard shows news scores predict the next day at least 55% of the time over 30 judgements.
  A coin already held is never tilted: its sell bar stays exactly what the laboratory measured. Every decision
  records the threshold it used and why (`TRADES.md`).
- **The AI budget goes where it changes a decision:** at each check the head trader ranks the coins where the
  rules would act (positions at risk first, then the strongest new signals) and spreads the day's budget over
  the day's remaining checks instead of spending it by noon. `/playbook` shows the last plan.
- **Knowledge passes to every agent:** the playbook (strategy, thresholds, lessons, the scorecard verdict, the
  last review, the lineage) is included in every Claude prompt and in the research agent's brief.
- **What it cannot do:** change the position caps, the loss brakes, the AI budget, the trading mode, or promote a
  strategy. It has no revenue target, on purpose: an agent pushed for fast revenue takes more risk. Its job is
  to make every dollar of AI budget change a decision and every threshold earn its evidence.

## The notebook: every strategy, trade and lesson as Markdown (for you, and for Claude)

Everything the desk does is in its SQLite journal, which code can read but people cannot. The notebook
(`data/notes/` on the server) is the same record rendered as Markdown, rebuilt after every market check,
so it can never disagree with the journal and can be rebuilt from any backup (`desk notes`).

| File | What it holds |
|---|---|
| `README.md` | what is in the folder, how to use it with Claude, the fixed guardrails and the laboratory bounds |
| `STRATEGY.md` | the champion with every parameter and its allowed range, its exam results, every challenger with the reason it passed or failed, the lineage of promotions and rollbacks, the lessons |
| `strategies/<id>.md` | one page per strategy card ever created: parameters, rationale and source, exam table, forward test, live record, and the exact `backtest` command that replicates it |
| `TRADES.md` | every trade: when and at what price, which strategy was live, the decision and its confidence, what each agent reported at entry (technical, prediction, news, forex), the checklist, the exit |
| `DIARY.md` | the notable moments in order: starts, buys and sells, failed orders, brakes, coin-list changes, protection problems, pace changes, lab results, promotions, rejections, rollbacks |
| `PERFORMANCE.md` | the report, the demo goals, the week-by-week learning curve (equity, trades, profit factor, champion), the Claude scorecard, costs |
| `weekly/<week>.md` | written once a week after the lab and never rewritten: the lab result, the lessons, Claude's review, the performance at that moment |

**Getting it:** `/notes` on Telegram sends the folder as a zip; the weekly backup message brings it too; on the
server `desk notes` rebuilds it and `desk notes --zip` makes the zip.

**Using it with Claude (outside the desk):** upload the zip to Claude and ask, for example, *"Read STRATEGY.md and
TRADES.md: what is failing, and which single change inside the bounds would you test first?"* The suggestions
come back to the laboratory as proposals; nothing in the notebook can change the desk by itself.

**Using it inside the desk:** `/review` makes Claude read the notebook and write a plain-language review (what works,
what fails, what the lab should test next, what would need new code, and how much the sample size allows one to
conclude). The same review is written into the weekly page. The research agent also receives the lineage of every
strategy already tried, with its result and the reason, so it builds on what passed instead of re-proposing what
failed. That is the learning curve: every week adds a page, and the next proposals start from it.

**Replication:** each strategy page carries the complete command to re-run its backtest on any computer with the
project, so a result can be checked by anyone, including a future version of the desk.

## Goals, and what success looks like

The desk has **no revenue target, on purpose**. An AI told to "make 5% a month" has one easy way to comply:
report more confidence, which unlocks more buys and more risk. So goals live in the code and in the reports
the owner reads, never in the AI's instructions (the prompt says the opposite: there is no return target,
never raise confidence to recover losses).

**Phase 1, the demo (now):** the daily report and `/report` show progress against these gates:

| Gate | Target | Why |
|---|---|---|
| Evidence period | at least 12 weeks | one month of results is noise (monthly swings of ~1.6% are normal) |
| Closed trades | at least 150 | with fewer, a 43% win rate could really be anything from 37% to 49% |
| Profit factor after fees | ≥ 1.3 | money won ÷ money lost, after Binance fees |
| Worst drop from a peak | ≤ 10% | the pain you would have to stomach with real money |
| Orders filled | 100% | technical reliability of the Binance connection |
| Return net of AI cost | > 0 | the AI bill is a real cost: $150/month is 1.5% of a $10,000 account every month |

The report also shows what simply holding would have done over the same period: Bitcoin, your watched
coins, and a "fair blend" that matches the desk's actual exposure (it is in cash most of the time, so 100%
buy-and-hold is a misleading yardstick in both directions). The likely range of the true win rate is shown
next to the observed one. If the 10% brake trips during the demo, the 12-week clock restarts.

**Phase 2, going live (not before Phase 1 passes):** start with a small amount, a written review of the
trade log, and the project-level goal below as the yardstick; any brake trip ends the phase.

**Project-level goal (owner's yardstick, reviewed monthly):** over 12 months, beat holding the same coins
or beat cash plus a stated margin, *after fees and AI costs*. Independent reviews of the backtests put a
realistic expectation for the free rule core at about 0 to +3% per half-year in rising markets and about 0
in falling ones, before AI costs, with 2-5% worst drops. Anything that promises more than ~3% a month should
be treated as a warning sign, not a target. Whether Claude adds value on top is measured, not assumed: the
report counts how often Claude chose differently from the rules, and their outcomes can be compared.

**The AI budget must fit the account.** At $10,000, Claude must add about 1.5 points per month over the
free rules just to pay for Opus-class usage; Haiku (about $5-15/month) is the only tier where that hurdle is
small. Choose the model in `python -m trader setup` with this arithmetic in mind, or run the free rules.

## Reading the report

| Metric | What it tells you |
|---|---|
| `execution_success_rate_pct` | Share of orders Binance confirmed as filled. **This is the "all trades successful" number**, and it should be 100% |
| `avg_price_gap_pct` | How far demo fill prices were from the real market. Small (under ~0.2%) means the demo results are realistic |
| `exits_by_exchange_orders` | Trades closed by the stop-loss / take-profit orders sitting on the exchange (instant), rather than by the desk at a check |
| `win_rate_pct` | Share of closed trades that made money |
| `profit_factor` | Money won divided by money lost. Above 1.0 means profitable; 1.5 or more is good |
| `max_drawdown_pct` | The worst drop from a high point, i.e. how painful the bad stretch was |
| `return_pct` | Total gain or loss against the starting balance |

The full history (every decision with its reasoning, every order, every closed trade) is in
`data/journal.db`, a SQLite file. You can open it with free tools such as *DB Browser for SQLite*.

---

## Important: what "successful trades" can and can't mean

There are two different kinds of success:

1. **Successful execution.** The order reached Binance and was filled at a known price.
   The system is built to make this 100%. It rounds sizes to Binance's rules, checks the
   minimum order value, confirms the fill, and after a network drop it asks Binance about the
   order instead of sending it again, so you don't end up with two orders.
2. **Profitable trades.** No system can make every trade profitable. A good strategy might win
   45–60% of its trades and still make money, because the stop-loss keeps losses smaller than wins
   (default: stop at 2× volatility, take profit at 3×). The demo phase exists to measure this
   honestly before any money is at risk.

## Known challenges and limitations

- **Location restrictions.** Real prices come from Binance's public market-data service, which
  works from anywhere. Demo *orders* are blocked from some countries, including the US, and from
  many US cloud servers. The free server guide uses Frankfurt, Germany.
- **The testnet isn't the real market.** It has thinner order books, its prices can drift from
  the real market, and Binance resets it from time to time. Analysis uses real prices, and every
  order records the real price next to the demo fill price, so the report shows the gap.
- **Spot only, no short selling.** The desk only makes money when prices rise. In a falling
  market it stays in cash, which is by design.
- **Forex data is daily** (European Central Bank rates), so it describes the general backdrop,
  not minute-to-minute moves.
- **Simple forecasts.** The Forecaster is a transparent trend model, not a black box. It's
  one vote among several, and you shouldn't treat it as a crystal ball.
- **AI cost, and how it's kept under control.** Checking 4× more often across up to 8 coins
  would multiply AI calls, so:
  - Claude is asked only when a coin shows a real buy or sell signal (neutral cases use the free rules).
  - Its view is reused while a coin's inputs haven't changed.
  - News is re-read every 30 minutes.
  - A **daily spending cap** (default $5) switches to the free rules until midnight UTC once it's reached.

  Typical monthly cost: Opus 5.5 $100–200, Sonnet 5.5 $50–100, Haiku 5.5 $5–15. The cap bounds the
  maximum. `/status` on Telegram shows today's spend. Without a key everything still works, for free.
- **Risk limits.** Besides the per-trade and daily limits, an overall brake stops new buys once the account
  is 10% below its starting balance (`MAX_DRAWDOWN_PCT`), until you review the report and reset or change it.
  Limits that trip are announced on Telegram.
- **Exchange orders have limits too.** A stop-loss *market* order fills at the best price available
  once the stop is touched: in a very fast crash that can be noticeably below the stop (the simulation
  assumes 0.2% slippage). Binance also rejects a protective pair if the market has already moved past
  one of its levels at placement time; the desk then sells at once or retries, and tells you on Telegram.
- **More coins means more correlated risk.** Most coins tend to fall together when Bitcoin falls.
  The Risk Manager still allows at most 3 open positions of 10% each, so watching 8 coins means more
  choice, not more exposure.
- **What the scout won't pick:**
  - Stablecoins and gold tokens are filtered out by list and by price behaviour.
  - Tokenized stocks are rejected: Claude must label each pick's asset type, and only "crypto" is accepted.
  - Coins that moved more than 30% in a day are never added (no chasing pumps).
  - In testnet mode, only coins the demo exchange lists at a realistic price are used.
- **Before going live** you need at least several weeks of testnet results across different
  market conditions, a review of the journal, and an explicit code change to allow live mode.

## Project layout

```
trader/
  agents/      scout, market, news, forex, technical, prediction, decision, mentor, risk, execution
  orchestrator.py   runs the cycle in order
  binance_client.py signed Binance REST calls
  llm.py            Claude calls with structured (JSON) answers
  journal.py        SQLite record of everything
  report.py         performance metrics
  notify.py         Telegram alerts and /status, /report commands
  setup_wizard.py   the setup questions (python -m trader setup)
  updater.py        over-the-air updates: fetch, test, restart, roll back (python -m trader update)
  backtest.py       replay the rule core on real history (python -m trader backtest)
  strategy.py       strategy cards: a complete configuration as data, with hard bounds
  lab.py            the laboratory: exam, forward test, promotion, rollback (python -m trader ... /lab)
  scorecard.py      Claude versus the rules, computed from real later prices
  notes.py          the Markdown notebook (data/notes): strategies, trades, diary, results (python -m trader notes)
  calibration.py    the threshold study: what buy threshold each coin has earned (head trader, weekly)
  agents/head.py    the head trader: AI budget pacing, per-check thresholds, the playbook, the weekly study
  health.py         /health, /agents, the pulse, the decision funnel and the stuck-rule alert
deploy/
  FREE_SERVER_GUIDE.md  click-by-click guide for a free 24/7 server
  setup.sh          one-command server installer
  desk              everyday server commands (desk status, desk report, ...)
tests/         automated tests (python -m pytest)
CHANGELOG.md   what each release number changed
```
