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

Think of it as a small trading firm where each employee has one job. (With the daily trend strategy, recommended
since 1.7.0, only Market Data, the Risk Manager and Execution work; see "The daily trend strategy" below.)

| Agent | Role in business terms | What it actually does |
|---|---|---|
| **Market Scout** | Finds new opportunities | Every 6 hours scans all ~770 Binance USDT coins and keeps the liquid crypto ones; stablecoins, gold tokens and tokenized stocks are filtered out. Claude then **searches the web** for today's news and catalysts and picks up to 8 coins to watch, each with a reason. Without Claude it adds the most-traded established coins |
| **Market Data** | The data clerk | Pulls hourly price candles for each watched coin from Binance |
| **News Analyst** | Reads the headlines, sets the pace | Reads CoinDesk, Cointelegraph and Decrypt. Claude rates each coin's news from -1 (bearish) to +1 (bullish) and advises how often to check the market right now. With no Claude key it uses a keyword list |
| **Event Synthesis** | Measures what prices did around news and scheduled releases | Stores every headline with its publication time, groups headlines about a coin into bursts, and for each burst and each scheduled US release (CPI, Fed statement, jobs report) measures afterwards the price jump, how long it took to register, what it gave back and whether volume dried up, next to the same hour on a normal day. Reported by `/events` and in the notebook; **it trades nothing** (see "Event synthesis") |
| **Strategy Library** | Tests published trading rules honestly | Seven well-known published rules (the Turtles' breakout, time-series momentum, moving-average crossovers, Connors' RSI(2), Bollinger bands, MACD, Larry Williams' breakout) and the desk's own two, written in-house from their sources and replayed on years of real prices under one fixed test: settings chosen on older years, judged on newer ones, walk-forward, and a luck check that counts every variant tried. On demand (`/library`), an occasional study rather than one of the agents in every check (so `/agents` does not count it); **it trades nothing** (see "The strategy library") |
| **Forex / Macro** | Watches the US dollar | Follows USD vs EUR, GBP, JPY and CHF (European Central Bank rates). A fast-rising dollar usually hurts crypto ("risk-off") |
| **Technical Analyst** | Chart reader | Standard indicators: trend averages (EMA), momentum (MACD), overbought/oversold (RSI), Bollinger bands, volatility (ATR) |
| **Chart Reader** | Describes the market picture | From the same finished candles: is each timeframe (1-hour, the 4-hour bars built from it, daily) in **chop** (going nowhere), **trending** or **impulsive** (a fast move)? Which structural events just happened: 12/26 average crossovers (50/200-day on the daily chart), a breakout above the recent high or a breakdown below the recent low, an unusually big candle, a new high with weaker momentum or on thinner volume? Nearest support and resistance. Shown to the Portfolio Manager as context, on `/chart` and in the notebook; nothing trades on it by itself (see "Chart events") |
| **Forecaster** | Predicts the next move | Fits a trend line to the last 24 hours, compares it with the noise, and gives a probability that the price goes up |
| **Portfolio Manager** | Makes the call | Weighs all reports and decides **BUY / SELL / HOLD**, with a confidence level and a plain-English reason. Uses Claude if a key is set, otherwise a weighted score |
| **Mentor** | The master traders' checklist | Every candidate buy must pass rules documented by well-known traders: above the 200-day average (Paul Tudor Jones), Bitcoin itself in an uptrend (don't fight the tape), no re-entry 24 h after a stop-loss (Livermore), at least 2:1 reward-to-risk (PTJ). Each line is shown with its source in the buy alert, and Claude is told to weigh it. Optionally `regime`: no buy while the 1-hour or 4-hour chart is in chop or right after a breakdown (off until a replay shows its effect) |
| **Risk Manager** | Safety officer, can veto anything | Sizes each trade so a stop-loss costs 1% of equity (the Turtle Traders' method), capped at 10% per coin; sets the stop-loss and a 2:1 take-profit; **trails the stop upward** as the price rises (Seykota: cut losses, let profits run). Blocks trades with low confidence, too many open positions, or after the 3% daily / 10% overall loss limits |
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

## Settings from your phone

`/setup` on Telegram changes the everyday settings with buttons: strategy, coins, risk level, budget, trading mode,
check interval, daily report hour and pulse. The desk cannot write its own settings file (the server's sandbox forbids
it, on purpose), so it hands the change to the update job, which checks every value again, keeps the previous file as
`data/.env.previous`, writes the new one, checks that the desk starts with it (else it puts the previous file back),
restarts the desk and reports the result. Keys and tokens are never asked for on Telegram; they are changed with
`desk setup` on the server. The update job trusts nothing the desk writes: only the exact values the desk's own
settings loader reads are accepted, and its files in the data folder are replaced, never written through a link.

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

## The daily trend strategy (recommended since 1.7.0)

**What it is.** One plain rule instead of the hourly agents: the desk holds a coin while its **daily closing price
is above its 50-day average**, and sells it the day after it closes below. It decides once a day, at the first
check after the daily close (00:00 UTC), on finished days only. Each coin gets an equal share of the account
(1 / number of coins), made smaller when the coin swings wildly (more than 50% a year), and never more than
`MAX_POSITION_PCT`. A 25% **emergency stop** sits on Binance for every position; after it fires, the coin is bought
again only once its trend has reset (a close below the average, then above it again). There is **no profit
target**: the strategy lives on a few long trends that pay for many small losses, so a winner is never cut short.
No AI decides anything, so it costs nothing to run. Switch it on with `desk setup` (step 5: strategy), or see
[deploy/FREE_SERVER_GUIDE.md](deploy/FREE_SERVER_GUIDE.md) "Switch to the daily trend strategy".

**Why.** Every replay of the hourly agents on two years of prices lost money after fees (about 200 variants, profit
factor 0.66-0.73): 4 to 11 small trades a week, each paying 0.2% in fees against a tiny edge. The daily trend rule
was the only idea that held up in every test the research ran: older years that were never used to choose it,
coins that were never used to choose it, and the most recent six months.

**What the replays showed** (real Binance daily prices; 8 coins BTC ETH SOL BNB XRP ADA DOGE LINK; one slot per
coin, at most 10% each when bought, the 25% stop and the desk's loss brakes; 0.1% fee per order plus slippage):

| Period | Trend rule | Just holding the 8 coins | Worst drop (rule) | Trades a week | Winners | Profit factor |
|---|---|---|---|---|---|---|
| Jan 2021 - Oct 2023 (boom and crash) | **+334%** | +390% | 40% | 1.7 | 23% | 1.9 |
| Nov 2021 - Dec 2022 (the crash alone) | **-22%** | -76% | 28% | 1.6 | - | 0.1 |
| Oct 2023 - Apr 2026 | **+192%** | +127% | 28% | 1.9 | 22% | 1.9 |
| Last 180 days (to Oct 2026) | **+19%** | +3% | 10% | 1.3 | 30% | 5.1 |
| Last 365 days (to Oct 2026) | **-4%** | -47% | 17% | 1.4 | 20% | 0.8 |

**How to read it (the business case).**
- It is a **crash-avoider that rides the long rises**, not a money machine. In a falling year it loses a little while
  holding loses half; in a strong boom it can trail holding (it sells on every dip below the average and buys back
  later, a little higher).
- It **wins only 1 trade in 4 or 5.** That is the design: a losing trade is a small step out; a winner is a trend
  held for weeks or months. Expect long runs of small losses; judge it on the profit factor and the result against
  holding, never on the win rate.
- **The worst drop is real money in a live account**: 28-40% in the replays (the hourly strategy's goal was 10%).
  The **Calmer** risk level in `desk setup` (`TREND_VOL_TARGET=0.25`) roughly halves the drops (17-24%) and keeps about
  half to two thirds of the gain (two thirds in Oct 2023 - Apr 2026, about half in the 2021-2023 boom).
- **Evidence comes slowly**: 1-2 trades a week means about 30 trades in 26 weeks. The trend goals below reflect that.
- Replays are history, not a promise. The research tested about 200 hourly variants and a handful of daily ones; the
  daily rule's results held across periods and coins, which is the best protection against a lucky fit that this
  kind of testing offers, and still no guarantee.

**What changes on the desk.** No market scout (the rule trades your `SYMBOLS` only), no news, forex, Claude or free-AI
opinions on trades, no head trader and no weekly strategy lab (they work on the hourly strategy). The desk still
checks every `CYCLE_MINUTES` to look after the protective orders, records everything and reports on Telegram. The
position slots and the 10% overall brake (with its 14-day pause) apply; the 3% daily-loss limit is checked but
practically never acts, because the rule buys only at the start of the day; the protection is the 25% emergency stop
on each position and the overall brake. The brake is measured from your starting budget, so in a bad stretch it will
usually trip (no new buys for 14 days, then half-size buys until the account is within 5% of the start); the replays
include it. When you switch,
coins you hold that are not in `SYMBOLS` are sold at the first daily decision; coins in the list are kept and follow
the rule from then on. `/trendtest [days]` on Telegram replays the rule on your coins over any period, also before
you switch. `MAX_OPEN_POSITIONS` should equal the number of coins: with fewer slots it buys the strongest coins first
and earned much less in the tests (3 slots: about +70% instead of +192% in Oct 2023 - Apr 2026).

**Two exchange details.** Binance refuses a sell order priced more than twice the current price, and its protective
pair always needs a take-profit price; so the desk sets it at 1.9x the price and **moves it up** whenever the price
comes within 30% of it. It is a placeholder, not a target. And if a coin only supports stop-limit orders, Binance
also refuses a stop that far below the price; the desk then watches that position itself every 5 minutes.

## Measuring the strategy before trusting it

**The daily trend rule:** `python -m trader backtest --strategy trend --days 1000` (on the server
`desk backtest --strategy trend --days 1000`; on servers installed before 1.7.0
`cd ~/crypto-desk && .venv/bin/python -m trader backtest --strategy trend --days 1000`) replays it on real daily
Binance prices with the desk's own Risk Manager. Flags: `--symbols`, `--balance`, `--position`, `--max-positions`,
`--sma`, `--vol-target`, `--trend-stop`. It uses the tested settings and these flags, not your settings file;
`/trendtest [days]` on Telegram uses yours. An independent check found it reproduces the research simulator behind
the table above to the cent on 11 scenarios (crashes, brake trips, 66 stops, scarce slots).

**The hourly agents:**

`python -m trader backtest --days 180` replays the desk's rule-based decision logic (indicators,
forecast, weighted score, the master traders' checklist, sizing, trailing stops, risk limits, fees,
exchange-side exits) over real hourly Binance history and prints return, trade count, win rate, profit
factor and drawdown next to the buy-and-hold result for the same coins and period. `--sweep` tries a grid
of thresholds and stop/take-profit distances; `--checks`, `--trail`, `--risk`, `--position`, `--symbols`
test the principles, sizing and coin sets (`--checks trend200,btc,cooldown,reward,regime` adds the chart reader's
`regime` rule, see "Chart events"). News, forex and Claude's judgement have no history and are left
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

For the published rules next to the desk's own under one strict protocol, see "The strategy library".

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

## The strategy library: published rules, tested honestly (/library)

The library answers one question: **do well-known, published trading rules beat simply holding the coins, on these
coins, after costs, once luck is taken into account?** It takes seven long-only rule families that traders and
researchers have published, writes each one here from its published description (the source is cited next to it),
and replays them on years of real Binance prices next to the desk's own two strategies. It is a measurement: **it
trades nothing**, and no setting, prompt or risk rule reads its result. A rule that passes is reported as a
*candidate*; running one would be a separate release you approve, as the daily trend rule was.

**Why a curated library and not "scraping GitHub and TradingView".** Those sites' terms forbid scraping, and the
desk never runs code it downloaded. A small library of published rules, each written and tested in-house, is
safer and, above all, measurable: every rule goes through the same simulator and the same fixed test.

| Rule | Source | Bars | Settings tried (headline first) |
|---|---|---|---|
| Donchian channel breakout (the Turtles' systems 1 and 2): buy a close above the 20 (55)-day high, stop 2 N below, sell below the 10 (20)-day low | Curtis Faith, *The Original Turtle Trading Rules* (2003) | daily | 20/10, 55/20 |
| Time-series momentum: hold while the price is above where it was 4 weeks ago, sized by its swing | Moskowitz, Ooi, Pedersen, *Journal of Financial Economics* 2012; Liu, Tsyvinski, *Review of Financial Studies* 2021 for crypto | daily | 28, 14, 91, 182 days |
| Moving-average crossover: hold while the short daily average is above the long one | Brock, Lakonishok, LeBaron, *Journal of Finance* 1992 (their five pairs) | daily | 1/50, 1/150, 5/150, 1/200, 2/200 |
| Connors' RSI(2): buy a dip (2-day RSI below 10 or 5) above the 200-day average, sell above the 5-day average | Connors & Alvarez, *Short Term Trading Strategies That Work* (2008) | daily | 10, 5 |
| Bollinger bands: buy a close below the lower band above the 200-day average, sell above the middle band (a control: Bollinger warns against exactly this) | John Bollinger, *Bollinger on Bollinger Bands* (2001) | daily | 20/2, 10/1.9, 50/2.1 |
| MACD: hold while the MACD line is above its signal line (and, in two variants, above zero) | Gerald Appel (2005); StockCharts ChartSchool | 4-hour | 12/26/9, 5/35/5, each with and without the zero filter |
| Larry Williams' volatility breakout: buy when the price rises above the open plus k x yesterday's range, sell at the close | Larry Williams, *Long-Term Secrets to Short-Term Trading* (1999) | daily | k = 0.5, 0.75, 1.0 |

The links are in `LIBRARY.md`. The desk's own **daily trend rule** and **hourly rules** run through their own replays
with their tested defaults, as references; and three yardsticks: **holding the same 8 coins**, **Bitcoin alone**,
**cash**.

**The test, fixed in code (`trader/library_stats.py`) before any rule was run on real prices** (protocol 1):

- The 8 coins the desk's research tested (BTC ETH SOL BNB XRP ADA DOGE LINK). Each coin joins 210 days after its first
  Binance candle (so every 200-day average exists from its first day): the period runs from 2018-03-15 (BTC, ETH) to
  the last finished day; SOL joins in March 2021.
- **Settings chosen on the first 70% of the days only** (the best "worse half": a setting must work in both halves of
  the window, with at least 5 rule trades a year), then **judged once on the last 30%** (the "judged period"), which the
  choice never saw.
- **Walk-forward:** choose on 2 years, test the next 6 months, step 6 months, and stitch the test months into one
  record that is entirely out of sample (13 segments, 2020-03 to the last day).
- The same simulator for every library rule: decisions on a finished bar's close, orders at the next open, 0.1% fee
  per order, 0.05% slippage (0.2% on stops), one slot per coin, at most 10% of the account per buy, long only, no
  leverage; every window starts flat and sells what it holds at its end (those closes do not count as the rule's
  trades).
- **The luck check** (the deflated Sharpe ratio of Bailey & Lopez de Prado, 2014) counts every variant tried: 23
  library settings plus the 2 references = 25. The more settings tried, the better the best one looks by luck alone,
  so the bar rises with them. "Luck about k in 100" means: if none of the variants had a real edge, a record at least
  this good would turn up by luck about k times in 100. It is not the chance that the rule is luck. The check is
  against cash; beating holding is measured, not luck-checked.
- **A rule PASSES only if all nine conditions hold:** an eligible setting in the first 70%; at least 30 rule trades in
  the judged period and 50 in the walk-forward; a positive Sharpe ratio and return in the judged period and in the
  walk-forward (beats cash); a higher Sharpe ratio than holding the coins in both (beats buy-and-hold, risk-adjusted); a
  worst drop no deeper than holding's in both; and a luck figure of 5 in 100 or fewer. Otherwise it FAILS, with every
  reason listed.
- A later change of any grid, threshold or cost is a new protocol version, and the luck check then also counts the
  25 variants of this one. Every saved run is kept (LIBRARY.md "Earlier runs"): a rule that passes once and fails in
  other runs is not evidence.
- The desk's own two strategies are **references, never candidates**: their settings were chosen by earlier research on
  some of these years (about 200 hourly variants, about 20 daily ones), so their figures are not a clean test; their
  luck figure counts those variants.

**What the full run found (2026-10-31, prices to 2026-10-30, version 1.9.0; two runs on the same prices gave the same numbers):**

| Rule | Setting chosen (first 70%) | Judged period: return / Sharpe / worst drop / trades | Walk-forward: return / Sharpe / worst drop / trades | Luck in 100 | Verdict |
|---|---|---|---|---|---|
| Hold the 8 coins | - | -16.6% / 0.20 / 66% / - | re-split every 6 months: +5,100% / 1.14 / 80% / - | - | yardstick (the bar) |
| Hold the 8 coins, bought once | - | (as above) | +1,966% / 0.96 / 86% / - | - | for information |
| Bitcoin alone | - | +9.0% / 0.30 / 53% / - | restarted every 6 months: +1,235% / 0.96 / 77% / - | - | yardstick |
| Cash | - | 0% / 0.00 / 0% / - | 0% / 0.00 / 0% / - | - | yardstick |
| Donchian / Turtle | entry 20, exit 10 | +38.3% / 0.61 / 30% / 117 | +1,169% / 1.32 / 32% / 244 | 12 | FAIL: luck about 12 in 100 |
| Time-series momentum | 14 days | +48.9% / 0.68 / 35% / 479 | +1,705% / 1.46 / 35% / 921 | 7 | FAIL: luck about 7 in 100 |
| Moving averages | 1/50 | +39.3% / 0.55 / 34% / 248 | +4,295% / 1.32 / 55% / 493 | 10 | FAIL: luck about 10 in 100 |
| RSI(2) dips | below 5 | +13.2% / 0.64 / 5% / 89 | +74% / 0.53 / 12% / 220 | 90 | FAIL: walk-forward Sharpe 0.53 below holding's 1.14 |
| Bollinger bands | 10 / 1.9 | +9.5% / 0.30 / 18% / 86 | +138% / 0.71 / 16% / 174 | 72 | FAIL: walk-forward Sharpe 0.71 below holding's 1.14 |
| MACD 4-hour | 12/26/9 above zero | +9.2% / 0.26 / 36% / 1,234 | +328% / 0.93 / 47% / 3,750 | 43 | FAIL: walk-forward Sharpe 0.93 below holding's 1.14 |
| Williams breakout | k 1.0 | -13.5% / -0.34 / 26% / 1,276 | +85% / 0.53 / 50% / 4,131 | 85 | FAIL: lost against cash in the judged period |
| Desk, daily trend rule (reference) | tested defaults | +45.1% / 0.62 / 30% / 248 | whole period: +5,495% / 1.25 / 40% / 660 | 8 (20 variants) | REFERENCE |
| Desk, hourly rules (reference) | tested defaults | -15.1% / -1.44 / 16% / 1,157 | last 3 years: -18.8% / -1.18 / 26% / 1,565 | 100 (200 variants) | REFERENCE |

Sharpe ratios in yearly terms; by luck alone the best of 25 variants would show about 0.86 a year. **No published rule
passed.** The three trend rules (Turtle breakout, momentum, moving averages) beat both cash and holding on the
Sharpe ratio, in the judged period and in the walk-forward, with shallower drops than holding; each fails only the
luck check (7 to 12 in 100 against the 5 or fewer required). The dip-buying rules, MACD and the Williams breakout did not
beat holding over the walk-forward. The desk's daily trend rule shows the same picture as the published trend rules
(luck about 8 in 100, not a clean test), and the hourly rules lost money again. Nothing changed on the desk.

**Which "holding" the walk-forward uses.** Each 6-month walk-forward segment starts flat, for the rules and for
holding alike (declared before the first run). For holding, that means the 8 coins are re-split equally every 6
months: +5,100%, Sharpe 1.14. The same coins bought once over the same dates made +1,966% (Sharpe 0.96, worst drop
86%). The verdicts use the re-split basket, the stricter bar in this run; the bought-once row is for information.
The Sharpe ratio is return per unit of swing: the Turtle breakout (+1,169%) and momentum (+1,705%) made less money than
coins bought once, but with drops of 32-35% instead of 86%.

**Cost.** On a 4-core laptop (Python 3.13) the full run took 42 seconds of CPU and 301 MB of memory at its peak (the
hourly reference is 30 of those seconds and about 190 MB); without the hourly reference 12 seconds and 115 MB. The
1/8-OCPU server is taken as 10-20 times slower: about 7-14 minutes of CPU, 20-40 minutes at the lowest priority. The
first run downloads about 434 public price pages (about 50 MB of JSON, 1-2 minutes here); later runs only what is new
(at most about 24 requests). The prices are kept in `data/library` (about 8 MB). `/library` runs it as a separate
process at the lowest CPU priority beside the desk, so the market checks come first; it skips the hourly reference
with a message when the server has less than 400 MB of free memory at that moment, and if the server still stops it
for memory, the message says so (`/library nohourly` then leaves out the hourly part).

**How to run it:** `/library` on Telegram is the recommended way (the result arrives as one message; `/library last`
shows the latest one again; `/library nohourly` without the hourly reference). On the server, `desk library` starts it
as a background service at the lowest CPU weight and shows its progress (Ctrl+C stops watching only; it keeps running
if you disconnect). The `desk` helper is installed by `deploy/setup.sh`, so servers installed before 1.9.0 do not have
`desk library`: use `/library` there (the manual `cd ~/crypto-desk && .venv/bin/python -m trader library --save
--low-priority` runs inside your login, so it competes with the desk at equal weight and stops if the connection
drops). Or `python -m trader library` on any computer with the project. Options: `--no-hourly` (without the hourly reference), `--refresh` (download every price
again), `--save` (store the result in the journal, for `/notes` and `/library last`). The full table, the sources, every
setting tried, the walk-forward choices and the caveats are in `LIBRARY.md` in the notebook.

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

## Chart events

Since 1.8.0 a **chart reader** (`trader/agents/chart.py`) describes the market picture of every watched coin at
every check, from the finished hourly candles the desk already downloads: the hourly chart, the 4-hour bars built
from those candles (aligned to 00:00, 04:00, ... UTC; a bar is used only once its four hours are finished) and the
daily closes. **It makes no new request**: the daily prices come from the download the checklist already makes for
coins the desk does not hold, so a coin you hold keeps its daily line for an hour after the buy and then reads
"daily: not refreshed while the coin is held" until the position closes (its hourly and 4-hour lines go on). With
`MENTOR_CHECKS=none` no daily prices are downloaded at all, and the daily line says so.

**The regime** is one number per timeframe, the *straightness* of the last 20 bars (the net move divided by the
length of the path the price took: 1 is a straight line, near 0 is going back and forth; a random walk scores about
0.22). Below 0.30 the chart is in **chop** (going nowhere); at 0.60 or more, when the 20 bars also moved at least
three usual ranges (3 ATR), it is **impulsive** (a fast move); everything in between is **trending**.

**The events**, on the latest finished bars (the last 6 hourly, the last 2 four-hour bars), each written to the
journal once per bar and kept 90 days:

| Event | Fires when |
|---|---|
| crossover | the 12-bar average crosses the 26-bar average (daily chart: the 50-day crosses the 200-day: golden / death cross) |
| breakout / breakdown | the bar closes above the highest high / below the lowest low of the bars before it: 2 days (48 bars) on the hourly chart, 5 days (30 bars) on the 4-hour chart |
| big candle | the bar's range is more than 2.5x the usual range (the ATR of the 14 bars before it) |
| weaker momentum | a new 2-day (5-day) closing high with the RSI at least 5 points below its value at the previous peak |
| thinner volume | a new closing high on average volume below 70% of the volume around the previous peak |

Where you see it: `/chart` (every coin: regimes, the newest events, support and resistance; `/chart eth` adds one
coin's recent events and levels), a short regime tag in each `/status` line, one line in the daily report with the
day's event counts, `CHART.md` in the notebook, and the diary (the 4-hour and daily crossovers, breakouts and
breakdowns only: the hourly chart fires about one event per seven hours per coin on pure noise, which would bury the
trades). The Portfolio Manager sees the picture as one more analyst report; **the rules' weighted score is unchanged**
and nothing trades on a chart event by itself.

**The `regime` rule** (off by default) is the one place the chart can refuse a buy: with
`MENTOR_CHECKS=trend200,btc,cooldown,reward,regime` the checklist refuses a buy while the hourly or the 4-hour chart is
in chop, or when the newest event on those charts is a breakdown. Measure it first, exactly as the live desk would
apply it: `python -m trader backtest --days 365 --checks trend200,btc,cooldown,reward,regime` runs the same agent on the
same candles; in the result `Checklist vetoes {'regime': n}` is how many buy signals it refused. The replay with the
rule takes a little longer (the chart is computed for every candidate buy, up to a few thousand times a year).

## Event synthesis: what prices do around news and scheduled releases (/events)

Since 1.8.1 the desk keeps a record of what prices actually did around scheduled US releases (the CPI inflation
figure, the Fed's FOMC statement, the monthly jobs report) and around bursts of headlines about a watched coin
(`trader/agents/events.py`). **It measures; it does not predict, and nothing trades on it**: no decision, threshold,
pace, risk rule or AI prompt reads these records (the study's diary lines are also kept out of the notebook digest
Claude's weekly review reads, because that review's summary reaches the decision prompt). It runs with the hourly
agents (`STRATEGY=hourly`); the daily trend rule never runs it (no downloads, nothing measured).

**What is measured, per coin and event, once the event is 4 hours old:** the largest move within 2 hours and whether
it counts as a **jump** (the rule below), its direction (from the price, never from the headline's tone), how many
minutes the jump took to register, the extreme and when it came, whether the price then gave back 38% of the move
within 4 hours (a parameter called "retrace", no claim beyond the number), whether trading volume fell back below the
pre-event level for 10 minutes, how busy the first 15 minutes were compared with the same 15 minutes on normal days,
and whether the price had already jumped in the hour **before** the event.

**Publication times trail the triggers.** A headline's time is when the feed published it, often after the move it
describes. So a headline event starts at the first headline of a burst (headlines about the same coin within 30
minutes of the first), and the hour before publication is checked: when the price had already jumped, the record
says "price moved first". Scheduled releases have exact times known in advance, which is why they are the main events.

**Compared with a normal day (the placebo).** Every event is paired with the same coin at the same New York clock time
on the nearest normal day before it (within 7 days; never a weekend for a weekday event, never a US market holiday,
never a Thursday for an 08:30 New York event because the weekly jobless claims come out then, and never a day the
same download cannot judge: it needs 3 normal days of its own before it, otherwise the next free day is used, or the
record says that no day within a week could be judged). For a release, a
normal day is one with no listed release: at 08:30 New York most weekdays still carry some second-tier US number, so
the comparison is "top-tier release versus a typical morning". For a headline burst it is an hour with no headlines
about the coin, which understates the normal rate (headlines often follow moves), so a headline "lift" is an upper
bound. Every summary shows the event rate next to the normal-day rate, with both counts; so do "price moved first",
the give-back and "volume dried up" (until a class has its own normal-day values, "moved first" is set against the
calibration: the rule fires on about 1 normal hour in 10).

**The jump rule is time-of-day aware.** A move counts as a jump when the 1-minute closes within 2 hours move more
than K times what the coin usually does in the same 2 hours on normal days: the baseline is the median, over the last
up to 10 normal days of the same kind, of the largest 2-hour move starting at the same New York clock time (one
download of 1,000 fifteen-minute candles, thirty-minute ones for a weekend event), and never less than 0.3%. K is
3.0 on weekdays and 4.5 at weekends (weekend prices sit still for hours, then move at once). The normal day is judged
against its own preceding normal days the same way. Why this matters: the hours before an 08:30 New York release are
the quiet overnight session while the two hours after it contain the US stock-market open, so a baseline taken from
the preceding hours called 45% of normal mornings "jumps" in a study of 63 real releases. Calibrated on real Binance
prices, the shipped rule calls **9.5% of the 189 normal weekday windows of that study** a jump (08:30 and 14:00 New
York), 7.5% of every weekday hour and 8.4% of every weekend hour of 62 days, and 21% of the release windows (CPI 19%,
jobs 10%, FOMC 37%). The typical largest 2-hour move was 1.92% on release days against 1.37% on the matched normal
days, and the first 15 minutes traded a median 4.6 times their normal volume (1.0 on the normal days). The "moved
first" hour uses the same rule against the hour before the same clock time on the same normal days (9% of normal
hours flag). Because the baseline is read from 15-minute closes, which miss part of the swings, it runs lower than the
1-minute moves next to it: `/events` therefore shows it as the bar a jump had to clear ("a jump at that hour needed
1.80%: 3 x 0.60%"), never as what a normal day moves; the matched normal day's own move is the like-for-like figure.

**Verdicts like the scorecard's.** No verdict under 30 events per class (`macro:cpi`, `macro:fomc`, `macro:nfp`, and
one class per headline category such as `hack-security`, `regulation`, `etf-flows`). From 30 on, the verdict says
whether these events jump more, less or no differently than the same hour on normal days (95% ranges), and whether a
desk that checks every 15 minutes could have looked before the jumps registered (the median minutes to register, next
to the median minutes to the extreme). A release class line says how many releases its coin-events come from (one
release gives one event per coin). Headline bursts less than 4 hours before or after a listed release are shown but
kept out of the headline classes, so a release is not counted twice: after it the burst sits in the release's window,
and before it ("Bitcoin steady ahead of CPI") the release's move sits in the burst's own window.

**The release calendar** is `trader/macro_calendar.json`, typed from the official pages (BLS CPI and Employment
Situation schedules, the Fed's FOMC calendar; US market holidays from the NYSE) and checked on 31 Oct 2026; it runs to
10 Dec 2027 and arrives with code updates. Times are UTC, converted from 08:30 / 14:00 New York with summer time
handled per date (a test checks every row with the time-zone database; a machine without one applies the US
summer-time rule itself for the New York clock hour). To add or remove a date without touching the
code, put the same format in `data/macro_calendar.json` next to the journal (a row with `"remove": true` deletes the
shipped one); a broken file is reported once in the diary and ignored, and a missing or broken calendar never stops
the desk (headline bursts are still measured). The desk warns in the diary when the calendar runs out within 60 days.

**Cost.** Three downloads per measured coin-event (600 one-minute candles around the event, the 1,000-candle baseline,
600 one-minute candles for the normal day; about 400 KB), at most 2 coin-events per check, under a daily budget
`EVENTS_DAILY_REQUESTS` (default 60 = 20 coin-events a day; `0` switches the study off; 1 or 2 is less than one
measured event, so the settings check reports it and the study stays off). Expected use is about 30
requests a day (about 10 headline bursts across 8 coins plus the releases); the first two or three days after the
update also measure the releases of the last 60 days. Releases are measured first; headline bursts the budget never
reaches are dropped after 7 days and counted (`/events` says how many, and tells events still inside their 4-hour
window from complete ones queued for measurement). A failed download pauses the study 30 minutes and tries the same
event again, but a coin Binance refuses (a name it does not know, such as a typo in `SYMBOLS`) is recorded as not
measured after one request, and an event whose download keeps failing for a whole day is recorded as not measured
too, so one coin can never hold up the others. Measuring two coin-events takes about 15 ms of
CPU on a laptop (well under a second on the small server) and 2 MB of memory while it runs; the candles are never
stored, only the result. Headlines are kept 180 days, measured events 400 days.

**Where you see it:** `/events` (the latest measured events in plain words and every class with its normal-day
comparison and verdict), `/events macro` (one line per release with every coin), `/events btc` (one coin), one line in
the daily report (with the release's date: the releases back-filled after an install are measured days later), and
`EVENTS.md` in the notebook. Nothing here changes a trade; a later release may propose a measured,
off-by-default checklist item `macro` (no new buys within 30 minutes of a release) once this record says it would have
mattered.

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
| `CHART.md` | the chart picture per coin now (regimes, support and resistance, the owner's line), the chart events of the last two weeks by day, a count per kind for the last week |
| `EVENTS.md` | what prices did around scheduled releases and headline bursts, event vs the same hour on a normal day: every class with its verdict, the releases and bursts measured, the fixed parameters, the calendar and the request budget |
| `LIBRARY.md` | the strategy library's latest result: every rule with its source, the settings tried and chosen, the judged-period and walk-forward results against holding and cash, the luck check, the verdicts, the caveats and how to reproduce it |
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

**With the daily trend strategy** (`STRATEGY=trend`) the gates are those of a slow strategy, counted from the day it
started (the hourly strategy's earlier trades do not count): at least **26 weeks** and **30 closed trades**, a profit
factor of at least **1.5**, a worst drop of at most **30%**, every order filled, and a positive return. Compare it
above all with "just holding" over the same weeks, shown in the same report.

The report also shows what simply holding would have done over the same period: Bitcoin, your watched
coins, and a "fair blend" that matches the desk's actual exposure (it is in cash most of the time, so 100%
buy-and-hold is a misleading yardstick in both directions). The likely range of the true win rate is shown
next to the observed one. If the 10% brake trips during the hourly demo, judge the 12 weeks again from that day (the
report does not restart the count by itself).

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
   (default: stop at 2× volatility, take profit at 4×); the daily trend rule wins only 20–30% by design, because its
   few long winners are many times its typical loss. The demo phase exists to measure this
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
  With the hourly agents the Risk Manager allows at most 3 open positions of 10% each, so watching 8 coins
  means more choice, not more exposure. The daily trend rule has one 10% slot per coin, so with 8 coins up to 80% of
  the account is invested in coins that tend to fall together: that is where its 28-40% worst drops come from
  (Calmer: 17-24%).
- **What the scout won't pick:**
  - Stablecoins and gold tokens are filtered out by list and by price behaviour.
  - Tokenized stocks are rejected three ways: by name (Binance calls them TICKER + B), by the exchange's own price rules (Binance holds stock, ETF, gold and fiat tokens to a much tighter price band than crypto coins, which also catches short names such as MUB), and by the AI's asset-type label (only "crypto" is accepted).
  - Coins that moved more than 30% in a day are never added (no chasing pumps).
  - In testnet mode, only coins the demo exchange lists at a realistic price are used.
- **Before going live** you need at least several weeks of testnet results across different
  market conditions, a review of the journal, and an explicit code change to allow live mode.

## Project layout

```
trader/
  agents/      scout, market, news, forex, technical, chart, events, prediction, decision, mentor, risk, execution
  orchestrator.py   runs the cycle in order
  binance_client.py signed Binance REST calls
  llm.py            Claude calls with structured (JSON) answers
  journal.py        SQLite record of everything
  report.py         performance metrics
  notify.py         Telegram alerts and /status, /report commands
  setup_wizard.py   the setup questions (python -m trader setup)
  updater.py        over-the-air updates: fetch, test, restart, roll back (python -m trader update)
  backtest.py       replay the rule core on real history (python -m trader backtest)
  trend.py          the daily trend rule and its replay (STRATEGY=trend, backtest --strategy trend, /trendtest)
  strategy.py       strategy cards: a complete configuration as data, with hard bounds
  lab.py            the laboratory: exam, forward test, promotion, rollback (python -m trader ... /lab)
  scorecard.py      Claude versus the rules, computed from real later prices
  notes.py          the Markdown notebook (data/notes): strategies, trades, diary, results (python -m trader notes)
  calibration.py    the threshold study: what buy threshold each coin has earned (head trader, weekly)
  agents/head.py    the head trader: AI budget pacing, per-check thresholds, the playbook, the weekly study
  health.py         /health, /agents, the pulse, the decision funnel and the stuck-rule alert
  agents/events.py  the event study: headlines with publication times, bursts, the release calendar, the measurement
  events_report.py  its texts: /events, the daily-report line, EVENTS.md
  library.py        the strategy library: seven published rules, one simulator, the protocol (python -m trader library)
  library_data.py   its prices: Binance's public candles, cached in data/library
  library_stats.py  how a rule is judged: the 70/30 split, the walk-forward, the luck check, the nine conditions
  library_report.py its texts: /library, the terminal table, LIBRARY.md
  macro_calendar.json  the scheduled US releases (official sources named inside; owner additions: data/macro_calendar.json)
deploy/
  FREE_SERVER_GUIDE.md  click-by-click guide for a free 24/7 server
  setup.sh          one-command server installer
  desk              everyday server commands (desk status, desk report, ...)
tests/         automated tests (python -m pytest)
CHANGELOG.md   what each release number changed
```
