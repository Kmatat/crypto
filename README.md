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
| **Market Data** | The data clerk | Pulls hourly price candles for each coin from Binance |
| **News Analyst** | Reads the headlines | Reads CoinDesk, Cointelegraph and Decrypt. Claude rates each coin's news from -1 (bearish) to +1 (bullish). With no Claude key it uses a keyword list |
| **Forex / Macro** | Watches the US dollar | Follows USD vs EUR, GBP, JPY and CHF (European Central Bank rates). A fast-rising dollar usually hurts crypto ("risk-off") |
| **Technical Analyst** | Chart reader | Standard indicators: trend averages (EMA), momentum (MACD), overbought/oversold (RSI), Bollinger bands, volatility (ATR) |
| **Forecaster** | Predicts the next move | Fits a trend line to the last 24 hours, compares it with the noise, and gives a probability that the price goes up |
| **Portfolio Manager** | Makes the call | Weighs all reports and decides **BUY / SELL / HOLD**, with a confidence level and a plain-English reason. Uses Claude if a key is set, otherwise a weighted score |
| **Risk Manager** | Safety officer, can veto anything | Sets trade size, stop-loss and take-profit. Blocks trades with low confidence, too many open positions, or after the daily loss limit is hit |
| **Execution Trader** | Places the order | Sends the order to Binance, confirms it was **FILLED**, and records the real quantity, price and fees |

Every hour (adjustable), for each coin:

```
prices ──► stop-loss / take-profit hit? ──yes──► SELL now
               │ no
               ▼
   Technical + Forecast + News + Forex reports
               ▼
   Portfolio Manager: BUY / SELL / HOLD + confidence
               ▼
   Risk Manager: approve or veto, set size and exits
               ▼
   Execution: place order ► confirm FILLED ► record in journal
```

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
- **AI cost.** With Claude enabled, each cycle makes about one call per coin plus one for the
  news, so roughly 100 calls a day on hourly cycles with 3 coins. Rough monthly cost: Opus 5.5
  about $60, Sonnet 5.5 about $30, Haiku 5.5 about $2. Without a key, everything still works,
  for free, using the rules.
- **Before going live** you need at least several weeks of testnet results across different
  market conditions, a review of the journal, and an explicit code change to allow live mode.

## Project layout

```
trader/
  agents/      market, news, forex, technical, prediction, decision, risk, execution
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
