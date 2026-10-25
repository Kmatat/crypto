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

**D) Claude key (optional).** Without it, built-in rules make the decisions for free.
Get one at **https://console.anthropic.com**, under **API Keys**. The account needs some prepaid credit.

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
| `/version` | Which code version is running, and whether a newer one is waiting |
| `/update` | Install the latest code now. It is tested first and the desk restarts itself; you get a message with the result |

Messages also arrive automatically:
- every buy, sell or failed order;
- when the scout changes the coin list;
- when the AI changes how often it checks;
- a daily report.

**On the server:** open Cloud Shell, run the same `ssh -i ssh-key-*.key ubuntu@YOUR_IP` line, and then:

| Command | What it does |
|---|---|
| `desk status` | Is it running? Latest activity and performance |
| `desk report` | Full performance report |
| `desk logs` | Watch the agents work live (Ctrl+C stops watching; the desk keeps running) |
| `desk setup` | Change keys, coins, Claude or Telegram settings |
| `desk stop` / `desk start` | Pause or resume trading |
| `desk reset` | Start the demo again from zero (old results are kept in a backup file) |
| `desk update` | Install the latest version now (normally not needed: it happens automatically) |
| `desk version` | Which version is running |

## Updates happen by themselves

When I publish improvements, your server notices within about 30 minutes, tests the new code, installs
it, restarts the desk and sends you a Telegram message such as *"🔄 Updated to version a1b2c3d and
restarted. What changed: ..."*. If a new version fails its tests or doesn't start, the server keeps the
old version and tells you. You never need to open the terminal for an update; `/update` on Telegram
installs one immediately if you don't want to wait.

## How to judge the demo after 3–4 weeks

| Figure | Good sign |
|---|---|
| Orders filled | 100%: the Binance connection is reliable |
| Demo vs real price gap | Under ~0.2%: demo results reflect the real market |
| Profit factor | Above 1.0 means it makes more than it loses; 1.5+ is good |
| Win rate | 45–60% is normal. Profit comes from wins being larger than losses |
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
  on Telegram. Keep those files; the free server could be reclaimed by Oracle one day.
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
account at risk per trade, at most 15% in one coin, 3 positions, 3% daily loss stop, 10% overall loss stop)
bound what a bad decision can cost.

## Troubleshooting

| Problem | Solution |
|---|---|
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
