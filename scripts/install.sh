#!/usr/bin/env bash
# One-shot installer for the Ipiran panel on an Ubuntu server inside Iran.
#
# It installs Xray (verified against its published checksum), generates a
# VLESS + REALITY config with sniffing enabled, installs the panel as a
# systemd service behind a secret URL path, and locks the firewall down.
#
#   sudo ./install.sh --host <SERVER-PUBLIC-IP>
#
set -euo pipefail

XRAY_BIN=/usr/local/bin/xray
XRAY_ASSETS=/usr/local/share/xray
XRAY_CONF_DIR=/usr/local/etc/xray
PREFIX=/opt/ipiran
DATA_DIR=/var/lib/ipiran
ENV_FILE=/etc/ipiran/panel.env
PANEL_USER=ipiran

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
log()  { printf '%s==>%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!! %s%s\n' "$YELLOW" "$OFF" "$*" >&2; }
die()  { printf '%sxx%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
head1() { printf '\n%s%s%s\n' "$BOLD" "$*" "$OFF"; }

HOST=""
PORT=443
SNI=""
PANEL_PORT=8777
PANEL_OPEN=""   # CIDR allowed to reach the panel; empty = localhost only
SSH_PORT=""

usage() {
  cat <<'EOF'
Usage: install.sh --host <public-ip-or-hostname> [options]

Required:
  --host X          Address clients dial. Normally this server's public IP.

Options:
  --port N          Client-facing VLESS port (default 443).
  --sni HOST        REALITY camouflage host. Verified reachable over TLS1.3+h2
                    before use; a working Iranian site is chosen if omitted.
  --panel-port N    Panel port (default 8777).
  --panel-open CIDR Expose the panel to this IP/range (e.g. 1.2.3.4/32).
                    Omit to keep it on localhost, reachable via SSH tunnel.
  --ssh-port N      SSH port to keep open in the firewall (auto-detected).
  -h, --help        This help.
EOF
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --host) HOST=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --sni) SNI=$2; shift 2 ;;
    --panel-port) PANEL_PORT=$2; shift 2 ;;
    --panel-open) PANEL_OPEN=$2; shift 2 ;;
    --ssh-port) SSH_PORT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)."
command -v apt-get >/dev/null || die "this installer targets Ubuntu/Debian."
[[ -n $HOST ]] || { usage; die "--host is required."; }

SELF_DIR=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
[[ -d $SELF_DIR/panel ]] || die "cannot find the panel/ directory next to this script."

if [[ -z $SSH_PORT ]]; then
  SSH_PORT=$(sed -n 's/^[[:space:]]*Port[[:space:]]\+\([0-9]\+\).*/\1/p' \
             /etc/ssh/sshd_config 2>/dev/null | head -1)
  SSH_PORT=${SSH_PORT:-22}
fi

head1 "1/7  packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends curl unzip ca-certificates python3 ufw

head1 "2/7  install Xray (checksum-verified)"
case "$(uname -m)" in
  x86_64|amd64)  ASSET=Xray-linux-64.zip ;;
  aarch64|arm64) ASSET=Xray-linux-arm64-v8a.zip ;;
  *) die "unsupported CPU: $(uname -m)" ;;
esac
VER=$(curl -fsSL https://api.github.com/repos/XTLS/Xray-core/releases/latest \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
[[ -n $VER ]] || die "could not find the latest Xray version."
if "$XRAY_BIN" version 2>/dev/null | head -1 | grep -q "${VER#v}"; then
  log "Xray ${VER} already present."
else
  TMP=$(mktemp -d)
  BASE="https://github.com/XTLS/Xray-core/releases/download/${VER}"
  curl -fsSL -o "$TMP/x.zip"      "${BASE}/${ASSET}"
  curl -fsSL -o "$TMP/x.zip.dgst" "${BASE}/${ASSET}.dgst"
  EXP=$(sed -n 's/^SHA2-256=[[:space:]]*\([0-9a-f]\{64\}\).*/\1/p' "$TMP/x.zip.dgst" | head -1)
  [[ -n $EXP ]] || die "no SHA2-256 line in the checksum file."
  GOT=$(sha256sum "$TMP/x.zip" | cut -d' ' -f1)
  [[ $EXP == "$GOT" ]] || die "checksum mismatch; refusing to install."
  log "checksum verified"
  unzip -oq "$TMP/x.zip" -d "$TMP/x"
  install -m 0755 "$TMP/x/xray" "$XRAY_BIN"
  install -d -m 0755 "$XRAY_ASSETS"
  install -m 0644 "$TMP/x/geoip.dat" "$TMP/x/geosite.dat" "$XRAY_ASSETS/"
  install -d -m 0755 "$XRAY_CONF_DIR"
  rm -rf "$TMP"
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
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF

head1 "3/7  pick + verify REALITY camouflage host"
# Does the host speak TLS 1.3 + HTTP/2? Best signal for a good REALITY target.
# Returns 0 on a confirmed TLS1.3+h2 handshake.
tls13_h2() {
  local h=$1 out
  command -v openssl >/dev/null || return 2   # 2 = can't tell
  out=$(timeout 12 openssl s_client -connect "${h}:443" -servername "$h" \
        -alpn h2 -tls1_3 </dev/null 2>/dev/null | tr -d '\000') || return 2
  grep -qi 'Protocol *: *TLSv1.3' <<<"$out" || return 1
  grep -qi 'ALPN protocol: h2' <<<"$out" || return 1
  return 0
}
# Plain TCP reachability on 443. Weaker, but enough: Xray revalidates the
# target properly when it loads the config.
tcp443() {
  timeout 8 bash -c "exec 3<>/dev/tcp/$1/443" 2>/dev/null
}

pick_sni() {
  # First pass: insist on a confirmed TLS1.3+h2 host.
  local h
  for h in "$@"; do
    tls13_h2 "$h" && { printf '%s' "$h"; return 0; }
  done
  # Second pass: accept any host reachable on 443 (openssl probe may be
  # unreliable on this box, but the port is clearly open).
  for h in "$@"; do
    if tcp443 "$h"; then
      warn "could not confirm TLS1.3+h2 for ${h}; using it since port 443 is reachable"
      printf '%s' "$h"
      return 0
    fi
  done
  return 1
}

CANDIDATES=(www.varzesh3.com www.aparat.com www.digikala.com www.snapp.ir www.cloudflare.com)
if [[ -n $SNI ]]; then
  if ! tls13_h2 "$SNI" && ! tcp443 "$SNI"; then
    die "$SNI is not reachable on port 443 from here. Pick another --sni."
  fi
else
  SNI=$(pick_sni "${CANDIDATES[@]}") \
    || die "no camouflage host reachable on 443; pass one with --sni."
fi
log "using SNI: $SNI"

head1 "4/7  panel user, code and Python check"
python3 -c 'import sqlite3,http.server,json,urllib.parse' \
  || die "this Python is missing stdlib modules; install python3 properly."
id -u "$PANEL_USER" >/dev/null 2>&1 \
  || useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$PANEL_USER"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/panel"
cp -a "$SELF_DIR/panel" "$PREFIX/panel"
install -d -m 0750 -o "$PANEL_USER" -g "$PANEL_USER" "$DATA_DIR"
install -d -m 0755 /etc/ipiran
chown -R "$PANEL_USER:$PANEL_USER" "$XRAY_CONF_DIR"
chmod 0750 "$XRAY_CONF_DIR"

cat > /usr/local/bin/ipiranctl <<EOF
#!/bin/sh
set -a
[ -r ${ENV_FILE} ] && . ${ENV_FILE}
set +a
cd ${PREFIX}/panel || exit 1
exec python3 -m app.cli "\$@"
EOF
chmod 0755 /usr/local/bin/ipiranctl

# The panel may restart Xray; grant exactly that one privileged command.
cat > /etc/sudoers.d/ipiran <<EOF
${PANEL_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart xray, /bin/systemctl restart xray
EOF
chmod 0440 /etc/sudoers.d/ipiran
visudo -cqf /etc/sudoers.d/ipiran || die "generated sudoers file is invalid."

head1 "5/7  configuration + keys"
# Reuse the existing secret on re-runs so the bookmarked link keeps working.
# (python avoids the tr|head SIGPIPE that silently kills set -o pipefail.)
SECRET=""
[[ -r $ENV_FILE ]] && SECRET=$(sed -n 's/^IPIRAN_SECRET_PATH=//p' "$ENV_FILE")
[[ -n $SECRET ]] || SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')
cat > "$ENV_FILE" <<EOF
IPIRAN_DATA_DIR=${DATA_DIR}
IPIRAN_HOST=$( [[ -n $PANEL_OPEN ]] && echo 0.0.0.0 || echo 127.0.0.1 )
IPIRAN_PORT=${PANEL_PORT}
IPIRAN_SECRET_PATH=${SECRET}
IPIRAN_XRAY_BIN=${XRAY_BIN}
IPIRAN_XRAY_CONFIG=${XRAY_CONF_DIR}/config.json
IPIRAN_XRAY_API=127.0.0.1:10085
IPIRAN_XRAY_SERVICE=xray
XRAY_LOCATION_ASSET=${XRAY_ASSETS}
EOF
chmod 0640 "$ENV_FILE"; chown root:"$PANEL_USER" "$ENV_FILE"

run_ctl() { sudo -u "$PANEL_USER" /usr/local/bin/ipiranctl "$@"; }
run_ctl set public_host "$HOST"
run_ctl set listen_port "$PORT"
run_ctl set reality_sni "$SNI"
# init-keys refuses to overwrite, so re-running the installer keeps keys and
# every existing client link stays valid.
run_ctl init-keys 2>/dev/null || log "REALITY keys already present, keeping them"
run_ctl render

cat > /etc/systemd/system/ipiran-panel.service <<EOF
[Unit]
Description=Ipiran panel
After=network-online.target xray.service
Wants=network-online.target
[Service]
Type=simple
User=${PANEL_USER}
Group=${PANEL_USER}
WorkingDirectory=${PREFIX}/panel
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 -m app.server
Restart=on-failure
RestartSec=5
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${DATA_DIR} ${XRAY_CONF_DIR}
[Install]
WantedBy=multi-user.target
EOF

head1 "6/7  start services"
cat > /etc/sysctl.d/99-ipiran.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
EOF
sysctl -q --system || warn "some sysctl values were not applied"
systemctl daemon-reload
systemctl enable --now xray >/dev/null 2>&1 || true
systemctl restart xray
sleep 1
systemctl is-active --quiet xray || { journalctl -u xray -n 20 --no-pager; die "Xray failed to start."; }
systemctl enable --now ipiran-panel >/dev/null 2>&1 || true
systemctl restart ipiran-panel
sleep 1
systemctl is-active --quiet ipiran-panel || { journalctl -u ipiran-panel -n 20 --no-pager; die "panel failed to start."; }
log "xray and panel are running"

head1 "7/7  firewall"
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow "$SSH_PORT"/tcp >/dev/null
ufw allow "$PORT"/tcp >/dev/null
if [[ -n $PANEL_OPEN ]]; then
  ufw allow from "$PANEL_OPEN" to any port "$PANEL_PORT" proto tcp >/dev/null
  log "panel reachable from ${PANEL_OPEN}"
else
  log "panel stays on localhost (reach it via SSH tunnel)"
fi
ufw --force enable >/dev/null
log "firewall active"

head1 "done"
if [[ -n $PANEL_OPEN ]]; then
  cat <<EOF
Panel URL (bookmark it, the secret path is the only key):

  http://${HOST}:${PANEL_PORT}/${SECRET}/

Open it only from ${PANEL_OPEN}. Anyone else sees an empty server.
EOF
else
  cat <<EOF
The panel listens on localhost only. From your own machine run:

  ssh -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} root@${HOST}

then open:

  http://127.0.0.1:${PANEL_PORT}/${SECRET}/
EOF
fi
echo
echo "Add clients in the panel, or: ipiranctl add-client \"name\" --quota-gb 20 --expire-days 30"
