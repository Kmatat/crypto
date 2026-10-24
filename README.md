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
| **Risk Manager** | Safety officer, can veto anything | Sets trade size, stop-loss and take-profit. Blocks trades with low confidence, too many open positions, or after the daily loss limit is hit |
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

Default weights for the rule-based decision: technical 35%, forecast 25%, news 25%, forex 15%.

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
   keys (from <https://testnet.binance.vision> → *Generate HMAC_SHA256 Key*), Claude (optional)
   and Telegram alerts (optional). It saves everything in `.env`. Run it again any time to change something.
3. *(Optional, recommended)* add Claude so it reads the news and makes the final decision with a written reason.
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
  agents/      scout, market, news, forex, technical, prediction, decision, risk, execution
  orchestrator.py   runs the cycle in order
  binance_client.py signed Binance REST calls
  llm.py            Claude calls with structured (JSON) answers
  journal.py        SQLite record of everything
  report.py         performance metrics
  notify.py         Telegram alerts and /status, /report commands
  setup_wizard.py   the setup questions (python -m trader setup)
deploy/
  FREE_SERVER_GUIDE.md  click-by-click guide for a free 24/7 server
  setup.sh          one-command server installer
  desk              everyday server commands (desk status, desk report, ...)
tests/         automated tests (python -m pytest)
```
