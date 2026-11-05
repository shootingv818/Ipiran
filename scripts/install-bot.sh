#!/usr/bin/env bash
# Installer for the FOREIGN relay server: Xray (client inbound + Iran
# outbounds, filled in later from Telegram) plus the Telegram control bot.
#
#   sudo ./install-bot.sh --token <BOT_TOKEN> --owner <YOUR_TELEGRAM_ID> \
#        --public-host <THIS_SERVER_IP>
#
# After it finishes, open the bot in Telegram, press Start, and add your
# Iranian exit server(s) by pasting their vless:// links.
set -euo pipefail

XRAY_BIN=/usr/local/bin/xray
XRAY_ASSETS=/usr/local/share/xray
XRAY_CONF_DIR=/usr/local/etc/xray
PREFIX=/opt/ipsaz
DATA_DIR=/var/lib/ipsaz
ENV_FILE=/etc/ipsaz/bot.env
USER_NAME=ipsaz

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
log(){ printf '%s==>%s %s\n' "$GREEN" "$OFF" "$*"; }
warn(){ printf '%s!! %s%s\n' "$YELLOW" "$OFF" "$*" >&2; }
die(){ printf '%sxx%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
head1(){ printf '\n%s%s%s\n' "$BOLD" "$*" "$OFF"; }

TOKEN="" OWNER="" HOST="" PORT=443 SNI="" SSH_PORT=""

usage(){ cat <<'EOF'
Usage: install-bot.sh --token X --owner N --public-host IP [options]

Required:
  --token X         Telegram bot token from @BotFather.
  --owner N         Your numeric Telegram id (get it from @userinfobot).
  --public-host IP  This (foreign) server's public IP or hostname.

Options:
  --port N          Client-facing VLESS port (default 443).
  --sni HOST        REALITY camouflage host; verified, auto-picked if omitted.
  --ssh-port N      SSH port to keep open (auto-detected).
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do case $1 in
  --token) TOKEN=$2; shift 2;; --owner) OWNER=$2; shift 2;;
  --public-host) HOST=$2; shift 2;; --port) PORT=$2; shift 2;;
  --sni) SNI=$2; shift 2;; --ssh-port) SSH_PORT=$2; shift 2;;
  -h|--help) usage; exit 0;; *) die "unknown arg: $1 (try --help)";;
esac; done

[[ $EUID -eq 0 ]] || die "run as root (sudo)."
command -v apt-get >/dev/null || die "targets Ubuntu/Debian."
command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] \
  || die "this server is not running systemd as PID 1 (no /run/systemd/system). This installer manages services with systemctl and needs a normal Ubuntu/Debian VPS, not a minimal container or chroot."
[[ -n $TOKEN && -n $OWNER && -n $HOST ]] || { usage; die "--token, --owner and --public-host are required."; }
[[ $OWNER =~ ^[0-9]+$ ]] || die "--owner must be the numeric id (digits only)."

SELF_DIR=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
[[ -d $SELF_DIR/bot ]] || die "cannot find the bot/ directory next to this script."
# Collects every port sshd actually listens on (it can have more than one
# "Port" line): the live socket first, then the main config, then Ubuntu
# 22.04+ style drop-ins under sshd_config.d/ that the main file may not
# mention at all. Getting this wrong before resetting the firewall is how
# an installer locks someone out of their own server.
collect_ssh_ports(){
  local -n _out=$1
  local p
  if command -v ss >/dev/null; then
    while read -r p; do
      [[ $p =~ ^[0-9]+$ ]] && _out+=("$p")
    done < <(ss -ltnp 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}')
  fi
  if [[ ${#_out[@]} -eq 0 ]]; then
    local files=(/etc/ssh/sshd_config)
    for f in /etc/ssh/sshd_config.d/*.conf; do
      [[ -f $f ]] && files+=("$f")
    done
    while read -r p; do
      [[ $p =~ ^[0-9]+$ ]] && _out+=("$p")
    done < <(cat "${files[@]}" 2>/dev/null \
              | sed -n 's/^[[:space:]]*Port[[:space:]]\+\([0-9]\+\).*/\1/p')
  fi
  if [[ ${#_out[@]} -eq 0 ]]; then
    _out=(22)
  fi
}

SSH_PORTS=()
if [[ -n $SSH_PORT ]]; then
  SSH_PORTS=("$SSH_PORT")
else
  collect_ssh_ports SSH_PORTS
fi
log "SSH port(s) to keep open: ${SSH_PORTS[*]}"

head1 "1/7  packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq \
  || die "apt-get update failed; check this server's network and apt mirrors."
# sudo: the bot drops root and calls back via `sudo -n systemctl restart
# xray`; also used below (sudo -u, visudo) during this install.
apt-get install -y -qq --no-install-recommends \
  curl unzip ca-certificates python3 ufw openssl sudo \
  || die "package installation failed; see the apt-get output above."

head1 "2/7  can this server reach Telegram?"
if curl -fsS --max-time 12 -o /dev/null "https://api.telegram.org"; then
  log "Telegram reachable"
else
  warn "api.telegram.org is NOT reachable from this server."
  warn "The bot cannot work here. Use a server/location that can reach Telegram."
  die "aborting: pick a foreign server with Telegram access."
fi

head1 "3/7  install Xray (checksum-verified)"
case "$(uname -m)" in
  x86_64|amd64) ASSET=Xray-linux-64.zip;;
  aarch64|arm64) ASSET=Xray-linux-arm64-v8a.zip;;
  *) die "unsupported CPU: $(uname -m)";;
esac
# Unauthenticated api.github.com calls are capped at 60/hour per source IP,
# which a shared-NAT VPS can trip; fall back to the releases/latest redirect
# (not subject to the same limit) if the API call comes back empty.
latest_xray_version(){
  local v
  v=$(curl -fsSL --max-time 15 \
      https://api.github.com/repos/XTLS/Xray-core/releases/latest 2>/dev/null \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  if [[ -z $v ]]; then
    v=$(curl -s --max-time 15 -o /dev/null -w '%{redirect_url}' \
        https://github.com/XTLS/Xray-core/releases/latest 2>/dev/null \
        | sed -n 's#.*/tag/##p')
  fi
  printf '%s' "$v"
}
VER=$(latest_xray_version)
[[ -n $VER ]] || die "cannot find latest Xray version (api.github.com and the releases redirect both failed; check this server's network)."
if "$XRAY_BIN" version 2>/dev/null | head -1 | grep -q "${VER#v}"; then
  log "Xray ${VER} already present."
else
  TMP=$(mktemp -d); BASE="https://github.com/XTLS/Xray-core/releases/download/${VER}"
  curl -fsSL -o "$TMP/x.zip" "${BASE}/${ASSET}"
  curl -fsSL -o "$TMP/x.zip.dgst" "${BASE}/${ASSET}.dgst"
  EXP=$(sed -n 's/^SHA2-256=[[:space:]]*\([0-9a-f]\{64\}\).*/\1/p' "$TMP/x.zip.dgst" | head -1)
  GOT=$(sha256sum "$TMP/x.zip" | cut -d' ' -f1)
  [[ -n $EXP && $EXP == "$GOT" ]] || die "checksum mismatch; aborting."
  log "checksum verified"
  unzip -oq "$TMP/x.zip" -d "$TMP/x"
  install -m 0755 "$TMP/x/xray" "$XRAY_BIN"
  install -d -m 0755 "$XRAY_ASSETS"
  install -m 0644 "$TMP/x/geoip.dat" "$TMP/x/geosite.dat" "$XRAY_ASSETS/"
  install -d -m 0755 "$XRAY_CONF_DIR"; rm -rf "$TMP"
  log "installed $("$XRAY_BIN" version | head -1)"
fi
cat > /etc/systemd/system/xray.service <<EOF
[Unit]
Description=Xray
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
Environment=XRAY_LOCATION_ASSET=${XRAY_ASSETS}
ExecStart=${XRAY_BIN} run -config ${XRAY_CONF_DIR}/config.json
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
[Install]
WantedBy=multi-user.target
EOF

head1 "4/7  REALITY camouflage host"
tls_ok(){ timeout 10 openssl s_client -connect "$1:443" -servername "$1" -alpn h2 -tls1_3 </dev/null 2>/dev/null | grep -qi 'ALPN protocol: h2'; }
tcp_ok(){ timeout 6 bash -c "exec 3<>/dev/tcp/$1/443" 2>/dev/null; }
if [[ -n $SNI ]]; then
  tls_ok "$SNI" || tcp_ok "$SNI" || die "$SNI not reachable on 443; pick another --sni."
else
  for c in www.cloudflare.com www.microsoft.com www.apple.com www.bing.com; do
    if tls_ok "$c" || tcp_ok "$c"; then SNI=$c; break; fi
  done
  [[ -n $SNI ]] || die "no camouflage host reachable; pass --sni."
fi
log "using SNI: $SNI"

head1 "5/7  bot user and code"
python3 -c 'import sqlite3,urllib.request,json,http.client' || die "python3 stdlib incomplete."
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$USER_NAME"
install -d -m 0755 "$PREFIX"; rm -rf "$PREFIX/bot"; cp -a "$SELF_DIR/bot" "$PREFIX/bot"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$DATA_DIR"
install -d -m 0755 /etc/ipsaz
chown -R "$USER_NAME:$USER_NAME" "$XRAY_CONF_DIR"; chmod 0750 "$XRAY_CONF_DIR"

cat > /usr/local/bin/ipsazctl <<EOF
#!/bin/sh
set -a; [ -r ${ENV_FILE} ] && . ${ENV_FILE}; set +a
cd ${PREFIX}/bot || exit 1
exec python3 -m app.cli "\$@"
EOF
chmod 0755 /usr/local/bin/ipsazctl

cat > /etc/sudoers.d/ipsaz <<EOF
${USER_NAME} ALL=(root) NOPASSWD: /usr/bin/systemctl restart xray, /bin/systemctl restart xray
EOF
chmod 0440 /etc/sudoers.d/ipsaz
visudo -cqf /etc/sudoers.d/ipsaz || die "bad sudoers file."

cat > "$ENV_FILE" <<EOF
IPSAZ_DATA_DIR=${DATA_DIR}
IPSAZ_BOT_TOKEN=${TOKEN}
IPSAZ_OWNER_ID=${OWNER}
IPSAZ_XRAY_BIN=${XRAY_BIN}
IPSAZ_XRAY_CONFIG=${XRAY_CONF_DIR}/config.json
IPSAZ_XRAY_API=127.0.0.1:10100
IPSAZ_XRAY_SERVICE=xray
IPSAZ_PORT=${PORT}
XRAY_LOCATION_ASSET=${XRAY_ASSETS}
EOF
chmod 0640 "$ENV_FILE"; chown root:"$USER_NAME" "$ENV_FILE"

head1 "6/7  keys and settings"
run_ctl(){ sudo -u "$USER_NAME" /usr/local/bin/ipsazctl "$@"; }
run_ctl set public_host "$HOST"
run_ctl set listen_port "$PORT"
run_ctl set reality_sni "$SNI"
run_ctl init-keys 2>/dev/null || log "REALITY keys already present, keeping them"

cat > /etc/systemd/system/ipsaz-bot.service <<EOF
[Unit]
Description=IP SAZ Telegram bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
WorkingDirectory=${PREFIX}/bot
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 -m app.bot
Restart=on-failure
RestartSec=5
# NoNewPrivileges is intentionally NOT set: the bot needs one sudo command
# (systemctl restart xray), which that flag would block.
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${DATA_DIR} ${XRAY_CONF_DIR}
[Install]
WantedBy=multi-user.target
EOF

head1 "7/7  services and firewall"
cat > /etc/sysctl.d/99-ipsaz.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
EOF
sysctl -q --system || warn "some sysctl values were not applied"
systemctl daemon-reload
systemctl enable xray >/dev/null 2>&1 || true   # started later, once an exit exists
systemctl enable --now ipsaz-bot >/dev/null 2>&1 || true
systemctl restart ipsaz-bot
sleep 2
systemctl is-active --quiet ipsaz-bot || { journalctl -u ipsaz-bot -n 20 --no-pager; die "bot did not start."; }
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
for p in "${SSH_PORTS[@]}"; do
  ufw allow "$p"/tcp >/dev/null
done
ufw allow "$PORT"/tcp >/dev/null
ufw --force enable >/dev/null
log "bot running, firewall active"

head1 "done"
cat <<EOF
Open Telegram, find your bot, and press Start (only your account works).

Next steps inside the bot:
  1. «افزودن سرور»  → paste the vless:// link of your Iranian exit server
     (from the Iran panel's "copy link" for its relay user).
  2. «کاربر جدید»    → create a user, get its QR/link, import in v2rayNG/Hiddify.

Xray does not start until at least one Iran exit is added — that is normal.

If a port still looks closed from outside despite ufw allowing it, also check
your hosting provider's own control-panel firewall / security group; that
sits in front of the OS and ufw cannot see or open it.
EOF
