#!/usr/bin/env bash
# One-command installer for the crypto trading desk on a fresh Ubuntu server.
#
#   bash ~/crypto-desk/deploy/setup.sh
#
# It installs Python, the app and its libraries, asks the setup questions,
# checks every connection, and starts the desk as a 24/7 background service
# that restarts by itself after a crash or a server reboot. Safe to run again.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="crypto-desk"
RUN_USER="$(id -un)"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$1" "$2"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$*"; exit 1; }

step "1/6" "Installing system packages (Python, git)..."
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  if ! { $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 >/dev/null; }; then
    warn "Package install had problems - continuing with what is already installed."
  fi
else
  warn "This installer is written for Ubuntu. Continuing, but you may need to install Python 3.10+ yourself."
fi
PY="$(command -v python3 || true)"
[ -n "$PY" ] || fail "Python 3 is not installed."
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "Python 3.10 or newer is needed (found $("$PY" -V)). Use Ubuntu 22.04 or 24.04."

# Small servers (1 GB memory) can run out of memory while installing libraries: add 1 GB of swap once.
if [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 1500000 ] && [ "$(awk 'NR>1' /proc/swaps | wc -l)" -eq 0 ] \
   && [ ! -e /swapfile ] && [ -d /run/systemd/system ]; then
  step "1/6" "Adding 1 GB swap memory (this server has little RAM)..."
  $SUDO fallocate -l 1G /swapfile && $SUDO chmod 600 /swapfile && $SUDO mkswap /swapfile >/dev/null \
    && $SUDO swapon /swapfile && echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null \
    || warn "Could not add swap - continuing."
fi

step "2/6" "Installing the trading desk in $APP_DIR ..."
cd "$APP_DIR"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.lock   # exact versions of every library, reviewed with the code
mkdir -p data
chmod 700 data                                  # the trade journal is nobody else's business

step "3/6" "Setup questions..."
if [ -d /run/systemd/system ] && systemctl is-active --quiet "$SERVICE"; then
  $SUDO systemctl stop "$SERVICE"  # pause a running desk while settings change; restarted in step 6
fi
if [ -f .env ]; then
  read -r -p "Settings already exist. Answer the setup questions again? (y/n) [n]: " again || again="n"
  case "$again" in [yY]*) .venv/bin/python -m trader setup ;; esac
else
  .venv/bin/python -m trader setup
fi

step "4/6" "Checking all connections..."
if ! .venv/bin/python -m trader check; then
  warn "Some checks failed (see [FAIL] lines above). The desk still starts and retries every cycle."
  warn "To fix keys later run:  desk setup"
fi

step "5/6" "Installing the 'desk' command..."
DESK_TMP="$(mktemp)"
sed "s|__APP_DIR__|$APP_DIR|g; s|__SERVICE__|$SERVICE|g" deploy/desk > "$DESK_TMP"
$SUDO install -m 755 "$DESK_TMP" /usr/local/bin/desk
rm -f "$DESK_TMP"

# Keep the operating system patched by itself (security updates nightly, reboot at 04:30 if a kernel
# update needs it; the desk starts again on boot and finishes its current check before stopping).
if command -v apt-get >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  $SUDO apt-get install -y -qq unattended-upgrades >/dev/null 2>&1 || warn "Could not install unattended-upgrades."
  $SUDO tee /etc/apt/apt.conf.d/51crypto-desk >/dev/null <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
APT
fi

step "6/6" "Starting the desk as a 24/7 service..."
if [ -d /run/systemd/system ]; then
  $SUDO tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=Crypto multi-agent trading desk (demo)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m trader run
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=60
TimeoutStopSec=600
UMask=0077
# Sandbox: the desk only needs the network and its own data folder. If the process were ever
# taken over (a bad dependency, a bug), it could not gain root, touch the system, edit its own code,
# or read the GitHub token and SSH keys.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$APP_DIR/data
InaccessiblePaths=-$RUN_HOME/.git-credentials -$RUN_HOME/.ssh
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF
  # --- over-the-air updates: a separate small job that may change the code (the desk itself cannot) ---
  $SUDO tee /etc/systemd/system/$SERVICE-update.service >/dev/null <<UNIT
[Unit]
Description=Crypto desk: fetch, test and install code updates from GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m trader update
Environment=PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
TimeoutStartSec=2700
Nice=10
# Sandbox: may change only the app folder (code, libraries, data); cannot gain root or touch the system.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$APP_DIR
InaccessiblePaths=-$RUN_HOME/.ssh
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=
UNIT
  # The updater cannot restart the desk itself (no privileges by design). It drops a flag file and this
  # root-owned helper, triggered by the file, performs the restart - the only privileged step, 3 lines long.
  $SUDO tee /etc/systemd/system/$SERVICE-restart.service >/dev/null <<UNIT
[Unit]
Description=Restart the crypto desk when the updater asks for it

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'rm -f $APP_DIR/data/restart-requested; systemctl restart $SERVICE.service'
UNIT
  $SUDO tee /etc/systemd/system/$SERVICE-restart.path >/dev/null <<UNIT
[Unit]
Description=Watch for a restart request from the crypto desk updater

[Path]
PathExists=$APP_DIR/data/restart-requested
Unit=$SERVICE-restart.service

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO tee /etc/systemd/system/$SERVICE-update.timer >/dev/null <<UNIT
[Unit]
Description=Check for crypto desk code updates every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
UNIT
  $SUDO tee /etc/systemd/system/$SERVICE-update.path >/dev/null <<UNIT
[Unit]
Description=Install a crypto desk update as soon as it is requested on Telegram

[Path]
PathExists=$APP_DIR/data/update-requested
Unit=$SERVICE-update.service

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO rm -f /etc/sudoers.d/$SERVICE   # earlier versions used a sudo rule; the restart helper replaces it
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE" "$SERVICE-update.timer" "$SERVICE-update.path" "$SERVICE-restart.path" >/dev/null 2>&1
  $SUDO systemctl restart "$SERVICE-update.timer" "$SERVICE-update.path" "$SERVICE-restart.path"
  .venv/bin/python -m trader update --check-only >/dev/null 2>&1 || true   # records the current version
  $SUDO systemctl restart "$SERVICE"
  sleep 3
  if systemctl is-active --quiet "$SERVICE"; then
    printf '\n\033[1;32m✅ The trading desk is running 24/7. You can close this window - it keeps running.\033[0m\n'
  else
    warn "The service did not start. Show the error with:  desk logs"
  fi
else
  warn "systemd is not available here, so no background service was installed."
  warn "Start the desk by hand with:  cd $APP_DIR && .venv/bin/python -m trader run"
fi

cat <<'EOF'

Everyday commands (type them on this server):
  desk status    is it running? latest activity and performance
  desk report    full performance report
  desk logs      watch the agents live (Ctrl+C to stop watching)
  desk setup     change keys or settings
  desk help      all commands

Updates: when new code is published, the server installs it by itself within about 30 minutes
(it runs the tests first and rolls back if anything fails) and tells you on Telegram.
Send /update to your bot to install right away, or /version to see what is running.
EOF
