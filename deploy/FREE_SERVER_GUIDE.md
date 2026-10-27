# Run the trading desk 24/7 on a free server

**Time needed:** about 45 minutes the first time, mostly waiting for accounts to be approved.
**Cost:** $0. The server, the Binance demo account and Telegram are free. Claude AI is optional and pay-per-use.

What you'll end up with: a small computer in the cloud that runs the agents around the clock,
places demo orders on Binance, and sends every trade and a daily report to your phone.

## Why this particular server

| Requirement | Why it matters | What we use |
|---|---|---|
| Free, with no time limit | The demo should run for weeks | **Oracle Cloud "Always Free"**: one small server, free for good |
| Located in a country Binance supports | Binance blocks trading API calls from the USA and some other countries, including most free US servers | Oracle lets you pick **Frankfurt, Germany** |
| Always on | The agents check the market every hour | The installer sets the desk to restart by itself after a crash or reboot |

Real *prices* work from anywhere: Binance publishes market data openly. Only the demo *orders*
need a supported country.

You'll need: an email address, a phone, and a credit or debit card. Oracle uses the card only
to verify your identity, and a free account is not charged. Prepaid and virtual cards are
often rejected.

---

## Part 1: Create the free Oracle account (10 minutes, then a wait)

1. Go to **https://www.oracle.com/cloud/free/** and click **Start for free**.
2. Enter your country, name and email, then confirm the email Oracle sends you.
3. Choose a password and a **Cloud Account Name** (any short name, such as `kmdesk`). Write it down,
   because you'll need it to sign in.
4. **Home Region: choose `Germany Central (Frankfurt)`.**
   ⚠️ This is the most important choice. You can't change it later, and the free server only
   exists in your home region. Don't choose a region in the USA, Canada, the UK or the Netherlands,
   because Binance restricts those countries.
5. Enter your address, phone number and card details, then finish.
6. Wait for the email **"Your Oracle Cloud account is fully provisioned"**. This usually takes 5–30 minutes.

## Part 2: Create the server (10 minutes)

1. Sign in at **https://cloud.oracle.com** with your Cloud Account Name, email and password.
2. Click the menu **☰** (top left), then **Compute**, then **Instances**, then **Create instance**.
3. **Name:** `crypto-desk`
4. In **Image and shape**:
   - Click **Change image**, choose **Ubuntu**, pick **Canonical Ubuntu 24.04**, and click **Select image**.
   - Click **Change shape**, choose **Specialty and previous generation**, tick **VM.Standard.E2.1.Micro**
     (it shows the label *Always Free-eligible*), and click **Select shape**.
5. **Networking:** keep the defaults. Make sure *Assign a public IPv4 address* is switched **on**.
6. **Add SSH keys:** choose **Generate a key pair for me** and click **Download private key**.
   A file such as `ssh-key-2026-10-24.key` is saved. **Keep it safe.** It's the key to your server.
7. Leave everything else as it is and click **Create**.
8. After 1–2 minutes the status turns green: **Running**. Copy the **Public IP address**
   (four numbers, such as `130.61.22.33`).

> **"Out of capacity" error?** Free servers are sometimes sold out in one data center.
> Under **Placement**, pick another *Availability domain* (AD-2 or AD-3) and try again, or try a few hours later.

## Part 3: Get your three keys ready (10 minutes)

**A) GitHub read-only key.** Your code repository is private, so the server needs permission to download it.
1. On **github.com**, click your profile photo (top right), then **Settings**.
2. In the left menu, near the bottom, click **Developer settings**, then **Personal access tokens**,
   then **Fine-grained tokens**, then **Generate new token**.
3. **Token name:** `crypto-server`. **Expiration:** 90 days.
4. **Repository access:** *Only select repositories*, then pick **Kmatat/crypto**.
5. **Permissions:** under *Repository permissions*, set **Contents** to **Read-only**.
6. Click **Generate token** and copy it (it starts with `github_pat_`). Keep it in a note for a few minutes.

**B) Binance demo-account keys.**
1. Go to **https://testnet.binance.vision** and click **Log In with GitHub**.
2. Click **Generate HMAC_SHA256 Key**, type any description, and click **Generate**.
3. Copy the **API Key** and the **Secret Key**. The secret is shown only once.

**C) Telegram bot (optional, recommended): phone alerts.**
1. Install Telegram on your phone if you don't have it.
2. Search for **@BotFather**, open the chat and send `/newbot`.
3. Give it a name (for example *My Crypto Desk*) and a username that ends in `bot` (for example `kmdesk_bot`).
4. BotFather replies with a **token** like `123456789:AAH...`. Copy it.

**D) Claude key (optional, paid).** Without it, a free AI (E) or the built-in rules make the decisions.
Get one at **https://console.anthropic.com**, under **API Keys**. The account needs some prepaid credit.

**E) A free AI key (optional, recommended on a small budget): Google Gemini.** It costs nothing and needs no card.
1. Open **https://aistudio.google.com** and sign in with your Google account (you must be 18 or older).
2. Accept the terms. Click **Get API key**, then **Create API key**, and copy the key.
3. Do **not** click *Set up billing*: the desk only uses the free tier.
4. Optional: **https://aistudio.google.com/rate-limit** shows your exact free limits (about 500 questions a day per
   model; the desk needs about 90 on a normal day).

Good to know: on the free tier Google may use what it reads to improve its models (not in the EU, the UK or
Switzerland). The desk sends it only public news headlines, market numbers and its strategy rules, never your
balances, keys or notebook. If you prefer a provider that never keeps what it reads, choose **Groq** in the setup instead
(**https://console.groq.com** → *API Keys* → *Create API Key*, no card).

## Part 4: Install the desk on the server (10 minutes)

1. In the Oracle console, click the **Developer tools** icon in the top bar and choose **Cloud Shell**.
   A black terminal window opens at the bottom of the page. The first time, this takes about a minute.
2. In the Cloud Shell title bar, open the **⚙ / ☰ menu**, click **Upload**, and select the
   `ssh-key-....key` file from Part 2.
3. Paste these two lines, replacing the IP address with yours from Part 2, and press Enter:
   ```
   chmod 600 ssh-key-*.key
   ssh -i ssh-key-*.key ubuntu@130.61.22.33
   ```
   To paste: **Ctrl+Shift+V** on Windows, **Cmd+V** on Mac, or right-click and choose Paste.
   When asked *"Are you sure you want to continue connecting?"*, type `yes`.
   You're on the server when the line starts with `ubuntu@crypto-desk`.
4. Paste these lines to download the desk:
   ```
   command -v git >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }
   git config --global credential.helper store
   git clone https://github.com/Kmatat/crypto.git ~/crypto-desk
   ```
   - **Username:** your GitHub username.
   - **Password:** paste the GitHub token from Part 3A. Nothing appears while you paste; that's normal.
     Press Enter.
5. Start the installer:
   ```
   bash ~/crypto-desk/deploy/setup.sh
   ```
   It installs everything (2–3 minutes), then asks four short questions:
   **trading mode** (choose 1, the demo account), your **Binance keys**, **Claude** (optional) and
   **Telegram**. It checks each key straight away and tells you if something is wrong.
6. At the end you'll see **"✅ The trading desk is running 24/7"**, and your Telegram receives
   *"🚀 Trading desk started"*.

You can now close the browser. The desk keeps running on the server.

---

## Everyday use

**From your phone (Telegram):**

| Send | You get |
|---|---|
| `/status` | Open positions (with 🛡️ when the stop-loss and take-profit are live on Binance), why each coin was bought or skipped, when the next check is, and today's Claude spend |
| `/report` | The performance figures |
| `/watchlist` | Which coins the desk watches, and why the scout picked each one |
| `/strategies` | The strategy in use, the challengers the lab is testing, and anything waiting for your approval |
| `/approve <id>` / `/reject <id>` | Switch to a challenger that passed every test, or dismiss it |
| `/lab` | Run the weekly strategy lab now (a few minutes; results arrive as a message) |
| `/notes` | The desk's notebook as a zip: every strategy, trade, lesson and result as Markdown pages. Upload it to Claude and ask what to improve |
| `/playbook` | What the head trader tells every agent (strategy, thresholds, lessons, verdicts) and how it spent the AI budget at the last check |
| `/health` | Is everything working? Version, last market check, errors, settings problems, updates, and what happened to every buy signal in the last 24 hours |
| `/agents` | How many agents are working and what each did; strategies created, promoted, rejected or rolled back; coins added or removed (`/agents 30` for 30 days) |
| `/trendtest` | The daily trend rule replayed on real prices with the desk's limits: result, worst drop and trades against just holding the coins (`/trendtest 1000` for 1,000 days). Works before you switch to it too |
| `/diagnose` | "Check the logs" from your phone: the settings in force, every buy signal per coin and what stopped it, order failures, hours the desk was not checking, and how many trades its own rules would have made on the same prices (`/diagnose 3` for 3 days) |
| `/ai` | Which AI answers first (the free AI or Claude), the free AI's questions used today and when its allowance restarts. `/ai free-first` saves money, `/ai claude-first` uses Claude's judgement while its budget lasts. Kept across restarts |
| `/aibudget` | Claude's daily budget, today's spend per agent and the month at today's pace. `/aibudget 3` lets Claude spend up to $3 a day (real money, at most $20), `/aibudget default` goes back to the settings file. Kept across restarts |
| `/review` | Claude reads the notebook and sends a plain-language review: what works, what fails, what the lab should test next. Uses a little of the AI budget |
| `/version` | Which code version is running, and whether a newer one is waiting |
| `/update` | Install the latest code now. It is tested first and the desk restarts itself; you get a message with the result |

Messages also arrive automatically:
- every buy, sell or failed order;
- when the scout changes the coin list;
- when the AI changes how often it checks;
- with the daily trend rule (from version 1.7.2): a short summary of each day's decisions just after 00:00 UTC (sent silently, so it
  does not wake you), and a message when a winner's take-profit order is moved up or a position bought by the
  hourly strategy is taken over;
- a daily report.

**On the server:** open Cloud Shell, run the same `ssh -i ssh-key-*.key ubuntu@YOUR_IP` line, and then:

| Command | What it does |
|---|---|
| `desk status` | Is it running? Latest activity and performance |
| `desk report` | Full performance report |
| `desk logs` | Watch the agents work live (Ctrl+C stops watching; the desk keeps running) |
| `desk setup` | Change keys, coins, Claude, the free AI or Telegram settings |
| `desk stop` / `desk start` | Pause or resume trading |
| `desk reset` | Start the demo again from zero (old results are kept in a backup file) |
| `desk update` | Install the latest version now (normally not needed: it happens automatically) |
| `desk version` | Which version is running |
| `desk notes` | Rebuild the notebook in `~/crypto-desk/data/notes` (add `--zip` for a zip file) |
| `desk backtest --strategy trend --days 1000` | Replay the daily trend rule (servers installed before 1.7.0: `~/crypto-desk/.venv/bin/python -m trader backtest --strategy trend --days 1000` from `~/crypto-desk`, or simply `/trendtest 1000` on Telegram) |

## Is it working? (no terminal needed)

The desk is quiet on purpose: it does not message you for every "hold". So that silence never hides a
problem, three things now reach your phone by themselves:

- **A pulse every 6 hours** (`PULSE_HOURS`): how many market checks ran, how many buy signals the rules
  produced and what happened to each (bought, refused by which rule, vetoed by Claude), and any errors.
- **Alarms**: a watchdog (a separate job, every 30 minutes) tells you if the desk stops checking the market,
  and restarts it if it stays stuck; one rule refusing every buy signal for a day gets its own alert;
  settings that make trading impossible are reported at start.
- **Update results**: every update ends with a message, including a failed or interrupted one, and the new
  desk confirms *"Update confirmed: the desk now runs version X"*.

Whenever you want to check yourself, send `/health`. The first line is the verdict: *"✅ All systems
working"* or *"🛑 Needs attention"* with the reason. `/agents` shows each agent's work and the strategy changes.

**"No trades" is not by itself a fault.** The desk buys only when the signals, the master traders' checklist
and the risk limits all agree; in a falling market (Bitcoin under its 200-day average) that can mean days in
cash. `/health` shows why each buy signal was refused. If one rule refused all of them, that is the
case to look at. With the daily trend rule a quiet week is normal too: it decides once a day and trades 1-2 times a
week; `/health` shows the date of its last daily decision and warns if it has not run.

## If the desk says your starting balance is small

The setup proposes 90% of the USDT in your Binance demo wallet as the desk's budget. A demo wallet with little USDT
gives a budget of a hundred dollars or so: positions of about $10 (the minimum order) and a desk that stops buying
after a small loss. (Since 1.5.0 a small balance no longer shrinks Claude's budget: on demo money Claude gets the
flat $5 a day, which `/aibudget` changes.) The money is fake, so give the demo a realistic budget:

1. On the server, run `desk setup`. At the first question choose **2) Paper trading** (real prices, simulated
   orders, no wallet limit) and type **10000** (or 5000) as the budget. Keep your other answers with Enter.
   (A bigger budget on **1) Demo account** works only if the Binance demo wallet holds that much USDT: Binance
   refuses orders the wallet cannot pay for, and the desk will tell you so instead of applying the budget.)
2. At the end the setup asks *Start a fresh journal?*. **y** starts a clean record with the new budget (the old one
   is kept as a backup file). **n** keeps the record and its history: when the desk starts, it adds the difference
   to its cash, like a deposit, and Telegram confirms it with *💰 Budget changed from ... to ...*.
3. Send `/health`: its first line shows the budget the desk really trades with. If it still shows the old amount,
   the 🛑 *Budget* line under it says why (usually: the demo wallet holds too little; choose paper trading).

## Add a free AI (no cost) to a running desk

Claude is paid in real dollars even though the trading money is fake. A free AI takes over the everyday
questions (reading the news, double-checking each buy or sell, choosing coins) at no cost:

1. Get a free Gemini key (Part 3, **E**). It takes two minutes.
2. On the server, run `desk setup` and press **Enter** to keep every answer until **STEP 3 - A free AI**.
   Answer **y**, choose **1) Google Gemini**, and paste the key. The setup asks the AI one test question.
3. If Claude is also set up, choose who answers first: **1) the free AI** (saves money; Claude only when the free AI
   cannot answer) or **2) Claude** (Claude's judgement while its daily budget lasts, then the free AI).
4. The desk restarts by itself. Send `/ai` on Telegram: it shows the free AI and its questions used today.

What stays with Claude whatever you choose: web research (the coin scout's news search and the weekly lab's), the
lab's strategy proposals and `/review`, because free tiers cannot search the web and the notebook holds your private
records. Without Claude, the scout works from market data only and the lab tests rule-based variations of the
strategy.

If the free AI's test question fails when you re-run `desk setup` (it may just be busy), answer **y** to *Keep the
saved free AI settings anyway?*: the desk retries by itself and tells you on Telegram if the key is wrong.

What to expect: the free model is smaller than Claude. The scorecard (daily report, `/report`) judges it
separately from Claude, so after a few weeks you can see which one's vetoes saved money. When the free allowance is
used up for the day, Claude (if it has budget) or the built-in rules answer until it restarts (for Gemini, at
midnight California time), and Telegram tells you once.

## Switch to the daily trend strategy

Since 1.7.0 the desk can trade one plain, tested rule instead of the hourly agents: hold a coin while its daily
close is above its 50-day average, sell after a close below, a 25% emergency stop on Binance, no AI (README "The daily
trend strategy" has the replays and the risks). To switch:

1. On Telegram, send `/trendtest 1000` first if you like: it replays the rule on real prices over the last 1,000 days
   with the desk's limits and answers in a minute (it changes nothing).
2. On the server, run `desk setup` and press **Enter** to keep every answer until **STEP 5 - Strategy, coins and
   timing**. Choose **1) Daily trend rule**.
3. Press **Enter** for the 8 tested coins (BTC ETH SOL BNB XRP ADA DOGE LINK). The setup gives each coin its own
   position slot (`MAX_OPEN_POSITIONS=8`) and switches the coin scout off.
4. Choose the risk level: **1) Normal** (as tested; worst drop in the replays 28-40%) or **2) Calmer** (worst drop about
   17-24%, about two thirds of the gain).
5. At *Start a fresh journal?* (asked only if you also changed the mode or budget) either answer is fine: the trend
   rule's goals count from the day it starts in any case.
6. The desk restarts. At its first check it makes the day's decisions: coins you hold that are not in the list are
   sold, coins in the list follow the rule, and coins above their average are bought. Telegram shows each order.
   After that it decides once a day, just after 00:00 UTC (02:00 in Central Europe in summer, 01:00 in winter).

What to expect: 1-2 trades a week, most of them small losses (1 trade in 4 or 5 wins), and now and then a winner held
for weeks or months that pays for them. `/report` shows the trend goals: 26 weeks, 30 trades, profit factor 1.5,
worst drop at most 30%, next to "just holding" the same coins. `/health` shows the date of the last daily decision.
To go back, run `desk setup` again and choose **2) Hourly agents**.

## One-time upgrade of a server installed before automatic updates

If your server was set up with the first version (before the update job existed), it does not yet know how to
update itself. One terminal session fixes that for good; afterwards every improvement arrives by itself.

1. Open the server as in Part 4, steps 1 to 3 (Cloud Shell, upload the key if it is no longer there, `ssh -i ...`).
2. Paste these two lines and press Enter:
   ```
   sudo systemctl stop crypto-desk
   cd ~/crypto-desk && git config --global credential.helper store && git pull --ff-only && bash deploy/setup.sh
   ```
   - If it asks for a **Username** and **Password**, give your GitHub username and paste a GitHub token
     (Part 3A; make a new one if the old one expired). The token is stored, so the update job can use it later.
   - When it asks *"Settings already exist. Answer the setup questions again? (y/n)"*, press **Enter** to keep
     your settings. Your keys, your Telegram pairing and your trading journal are kept; the journal is upgraded
     in place.
3. Wait for **"✅ The trading desk is running 24/7"**. Your Telegram receives *"🚀 Trading desk started"* with the
   new command list. Send `/version`: it shows the new version and *"Up to date"*.

That is all. From now on `/version`, `/update` and the automatic 30-minute check work as described below.

| If you see | Do this |
|---|---|
| `error: Your local changes to the following files would be overwritten` | A code file on the server was edited by hand. Run `git -C ~/crypto-desk checkout -- .` and paste the two lines again (your `.env` and `data/` folder are never touched by this) |
| `fatal: Authentication failed` | Paste a GitHub **token** as the password, not your GitHub password; the token needs *Contents: Read-only* on Kmatat/crypto |
| `fatal: Not possible to fast-forward` | Run `git -C ~/crypto-desk fetch && git -C ~/crypto-desk reset --hard origin/HEAD` and paste the two lines again |

## Updates happen by themselves

When I publish improvements, your server notices within about 30 minutes, tests the new code, installs
it, restarts the desk and sends you a Telegram message such as *"🔄 Updated to version 1.3.0 (a1b2c3d) and
restarted. What changed: ..."*. The release numbers are explained in `CHANGELOG.md`. If a new version fails its tests or doesn't start, the server keeps the
old version and tells you. You never need to open the terminal for an update; `/update` on Telegram
installs one immediately if you don't want to wait.

## How to judge the demo after 3–4 weeks

| Figure | Good sign |
|---|---|
| Orders filled | 100%: the Binance connection is reliable |
| Demo vs real price gap | Under ~0.2%: demo results reflect the real market |
| Profit factor | Above 1.0 means it makes more than it loses; 1.5+ is good |
| Win rate | 45–60% is normal for the hourly agents; 20–30% for the daily trend rule. Profit comes from wins being larger than losses |
| Worst drop from peak | The pain you'd have to stomach with real money. Lower is better |

## Good to know

- **Oracle may switch off "idle" free servers.** If a free server uses very little processing power
  for 7 days, Oracle can stop it, and this desk uses very little. You'll notice because the daily
  Telegram report stops arriving. Fix it by going to Compute, then Instances, then crypto-desk, and
  clicking **Start**; the desk restarts by itself. To prevent it, upgrade the account to
  *Pay As You Go* under Billing. Always Free servers stay free after upgrading, but set a $1 budget
  alert to be safe.
- **Binance resets the demo exchange** from time to time, roughly monthly. Balances return to
  default and sometimes the keys stop working. You'll get a Telegram alert (the desk also notices when
  coins it thought it held have vanished). Create new keys at testnet.binance.vision, then run
  `desk setup`, and `desk reset --force` for a clean restart.
- **Your trading journal is backed up to your phone** once a week: the bot sends you the journal file
  on Telegram, together with the notebook (the same facts as readable Markdown pages). Keep those files;
  the free server could be reclaimed by Oracle one day, and the notebook is what you give to Claude when
  you want a second opinion on the strategy.
- **The GitHub token expires** after 90 days. It's needed only for downloading updates, and the
  desk keeps trading without it; `/version` will mention when checks start failing. Create a new
  token the same way, then on the server run `git -C ~/crypto-desk fetch` once and paste it.
- **Security:** your keys are stored only on your server, in a file only your user can read. The
  Binance keys are demo keys with fake money. Never put real-money Binance keys into this demo.

## Security checklist (10 minutes, do it once)

The server holds only demo keys and fake money, but it is connected to real accounts. Protect those:

| Account | What to do | Why |
|---|---|---|
| **Oracle Cloud** | Keep multi-factor authentication on (Oracle asks for it at signup). Under *Billing → Budgets*, create a budget of $1 with an email alert. A budget only **emails** you, it does not stop charges, so if you ever upgrade to Pay As You Go, glance at the Billing page after the first month | This account has your credit card. The free tier itself cannot be charged |
| **GitHub** | Use 2FA. This account matters most: the server installs whatever code is on the repository, and your Binance demo login is "Log in with GitHub". Check that only you are listed under *Settings → Collaborators* of the repository, and that no deploy keys exist. The token you created is read-only for one repository and expires in 90 days | Whoever controls the repository controls the code the server runs |
| **Binance** | Your demo keys belong to the testnet, not to a real Binance account; only ever paste **testnet** keys into this desk. If you have a real Binance account, it is not connected to this desk at all | The desk is wired to the demo exchange only, so real keys would not even work, but they should never sit on a server |
| **Telegram** | Turn on *Settings → Privacy and Security → Two-Step Verification* in your Telegram app, and a passcode lock on the phone. The bot pairs with the one-time code the setup shows you (automatic). The desk will **never** ask you to type a key, a password or to open a link in Telegram: if a message does, assume the bot token leaked, send `/revoke` to @BotFather and run `desk setup` with the new token | Your Telegram chat holds the full trading history and is the remote control |
| **Claude (Anthropic)** | Use a strong unique password (and two-factor login if your account offers it). In the console, set a monthly spending limit (e.g. $150) on top of the desk's own daily cap, and delete the key when you stop the demo | Two independent caps on the only part of this setup that costs money |
| **Server** | Keep the SSH key file somewhere safe (not in a shared or synced folder) and never email it. When you finish in Cloud Shell, you can delete the copy you uploaded there (`rm ~/ssh-key-*.key`), or keep it knowingly behind Oracle's MFA. Nothing else to install: Oracle allows only SSH in, the desk only makes outgoing connections, it runs in a sandbox that can't change the system, and the OS installs its own security updates nightly | A lost key file is the only way someone else gets onto the server |

What is already handled for you: your keys are stored on the server in a file only your user can read; they
never appear in the logs or in Telegram messages; the code refuses to run in any real-money mode; links in
AI-written text are stripped before anything reaches your phone; and the Risk Manager's limits (1% of the
account at risk per trade, at most 10% in one coin, 3 positions, 3% daily loss stop, 10% overall loss stop)
bound what a bad decision can cost.

## Troubleshooting

| Problem | Solution |
|---|---|
| Telegram still shows the old budget after you changed it | Since 1.6.2 the desk applies a new STARTING_BALANCE_USDT at its next start and confirms it (💰). If it says 🛑 *Budget not changed*, the Binance demo wallet holds less than the new budget: run `desk setup` and choose **2) Paper trading** with the budget you want |
| The daily trend rule bought nothing | It buys only coins whose daily close is above their 50-day average; in a falling market that can be none for weeks (holding cash is the point). `/status` shows each coin's latest decision and why; a coin whose emergency stop fired waits until its trend has reset |
| Card rejected at Oracle signup | Use a regular credit or debit card whose name and address match what you entered |
| `Out of capacity` when creating the server | Choose a different Availability domain, or try later |
| `ssh: connect ... timed out` | Check the server shows *Running*, and that you used the **Public** IP |
| `Permission denied (publickey)` | Use the key file downloaded in Part 2, and the user name `ubuntu` |
| `git clone` says authentication failed | Paste the GitHub **token** as the password, not your GitHub password. Check the token has *Contents: Read-only* on Kmatat/crypto |
| Setup says Binance *restricted location* | The server's country is blocked. Choose paper trading (real prices, simulated orders), or make a new Oracle account with home region Frankfurt |
| No Telegram messages | Run `desk check`. In Telegram, open your bot and press **START**, then run `desk setup` again |
| Anything else | Run `desk logs` and send me (Claude) the last lines you see |

**To remove everything:** in the Oracle console go to Compute, then Instances, then crypto-desk,
then **More actions**, then **Terminate**, and tick *Permanently delete the boot volume*.
