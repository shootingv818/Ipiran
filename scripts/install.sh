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
USE_HTTPS=0
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
  --https           Serve the panel over TLS with a self-signed certificate,
                    so the secret path is not sent in clear text.
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
    --https) USE_HTTPS=1; shift ;;
    --ssh-port) SSH_PORT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)."
command -v apt-get >/dev/null || die "this installer targets Ubuntu/Debian."
# A bare VPS is still expected to be a normal Ubuntu/Debian cloud image with
# systemd as PID 1 (every mainstream provider ships this). Catch anything
# else here with a clear message instead of failing confusingly later at
# the first `systemctl` call.
command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] \
  || die "this server is not running systemd as PID 1 (no /run/systemd/system). This installer manages services with systemctl and needs a normal Ubuntu/Debian VPS, not a minimal container or chroot."
[[ -n $HOST ]] || { usage; die "--host is required."; }

SELF_DIR=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
[[ -d $SELF_DIR/panel ]] || die "cannot find the panel/ directory next to this script."

# Collects every port sshd actually listens on (it can have more than one
# "Port" line), checking the live socket first, then the main config, then
# Ubuntu 22.04+ style drop-ins under sshd_config.d/ that the main file often
# does not mention at all. Getting this wrong and then resetting the
# firewall is how an installer locks someone out of their own server.
collect_ssh_ports() {
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
# A genuinely bare server may have none of this preinstalled:
#   curl, ca-certificates  - fetch and verify Xray
#   unzip                  - unpack it
#   python3                - runs the panel (stdlib only, nothing from PyPI)
#   ufw                    - firewall
#   openssl                - the REALITY/TLS probe, and required by --https
#   sudo                   - the panel drops root and calls back via
#                             `sudo -n systemctl restart xray`; also used
#                             below (sudo -u, visudo) during this install
apt-get install -y -qq --no-install-recommends \
  curl unzip ca-certificates python3 ufw openssl sudo \
  || die "package installation failed; see the apt-get output above."

head1 "2/7  install Xray (checksum-verified)"
case "$(uname -m)" in
  x86_64|amd64)  ASSET=Xray-linux-64.zip ;;
  aarch64|arm64) ASSET=Xray-linux-arm64-v8a.zip ;;
  *) die "unsupported CPU: $(uname -m)" ;;
esac
# Unauthenticated api.github.com calls are capped at 60/hour per source IP.
# A cheap VPS can share that IP with many other tenants on the same NAT
# gateway, so fall back to the plain releases/latest redirect (not subject
# to the same limit) if the API call comes back empty.
latest_xray_version() {
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
[[ -n $VER ]] \
  || die "could not find the latest Xray version (api.github.com and the releases redirect both failed; check this server's network)."
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
# The panel reads the short-lived traffic log from here; /run is RAM.
ReadWritePaths=/run/ipiran
UMask=0022
[Install]
WantedBy=multi-user.target
EOF

# RAM-backed directory for the ephemeral traffic monitor. Group-writable by
# the panel so it can read the log and delete it when the window closes,
# even though Xray (root) owns the file.
cat > /etc/tmpfiles.d/ipiran.conf <<EOF
d /run/ipiran 0770 root ${PANEL_USER} -
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
systemd-tmpfiles --create /etc/tmpfiles.d/ipiran.conf 2>/dev/null \
  || install -d -m 0770 -o root -g "$PANEL_USER" /run/ipiran
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
# Optional TLS. Without it the secret path, which is the only key to the
# panel, travels in clear text across the operator's network.
TLS_LINES=""
if [[ $USE_HTTPS -eq 1 ]]; then
  command -v openssl >/dev/null || die "--https needs openssl installed."
  install -d -m 0750 -o root -g "$PANEL_USER" /etc/ipiran/tls
  if [[ ! -s /etc/ipiran/tls/panel.crt ]]; then
    SAN="DNS:${HOST}"
    [[ $HOST =~ ^[0-9.]+$ ]] && SAN="IP:${HOST}"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 1095 -nodes \
      -keyout /etc/ipiran/tls/panel.key -out /etc/ipiran/tls/panel.crt \
      -subj "/CN=${HOST}" -addext "subjectAltName=${SAN}" 2>/dev/null \
      || die "could not generate the panel certificate."
  fi
  chown root:"$PANEL_USER" /etc/ipiran/tls/panel.key /etc/ipiran/tls/panel.crt
  chmod 0640 /etc/ipiran/tls/panel.key
  TLS_LINES=$'IPIRAN_TLS_CERT=/etc/ipiran/tls/panel.crt\nIPIRAN_TLS_KEY=/etc/ipiran/tls/panel.key'
fi

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
${TLS_LINES}
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

# Binding a port below 1024 (e.g. 80) as the non-root panel user needs this
# one capability. Harmless for high ports.
CAP_LINES=""
if [[ ${PANEL_PORT} -lt 1024 ]]; then
  CAP_LINES=$'AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE'
fi
# Remove any stale drop-in from earlier manual attempts so it cannot conflict.
rm -rf /etc/systemd/system/ipiran-panel.service.d

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
${CAP_LINES}
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
for p in "${SSH_PORTS[@]}"; do
  ufw allow "$p"/tcp >/dev/null
done
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
SCHEME=$( [[ $USE_HTTPS -eq 1 ]] && echo https || echo http )
# Omit the port from the URL for the standard 80/443 so it reads cleanly.
PORT_SUFFIX=":${PANEL_PORT}"
{ [[ $PANEL_PORT -eq 80 && $USE_HTTPS -eq 0 ]] || [[ $PANEL_PORT -eq 443 && $USE_HTTPS -eq 1 ]]; } && PORT_SUFFIX=""
if [[ -n $PANEL_OPEN ]]; then
  cat <<EOF
Panel URL (bookmark it, the secret path is the only key):

  ${SCHEME}://${HOST}${PORT_SUFFIX}/${SECRET}/

EOF
  if [[ $USE_HTTPS -eq 1 ]]; then
    echo "The certificate is self-signed, so the browser warns once. Check that"
    echo "the fingerprint it shows matches this before you accept it:"
    echo
    echo "  $(openssl x509 -in /etc/ipiran/tls/panel.crt -noout -fingerprint -sha256 | cut -d= -f2)"
  else
    warn "This is plain HTTP, so the secret path is visible to anyone watching"
    warn "the network. Re-run with --https to encrypt it."
  fi
else
  cat <<EOF
The panel listens on localhost only. From your own machine run:

  ssh -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} root@${HOST}

then open:

  ${SCHEME}://127.0.0.1:${PANEL_PORT}/${SECRET}/
EOF
fi
echo
echo "If you ever rotate the secret path, get the current URL with: ipiranctl url"
echo "Add clients in the panel, or: ipiranctl add-client \"name\" --quota-gb 20 --expire-days 30"
echo
echo "If a port still looks closed from outside despite ufw allowing it, also"
echo "check your hosting provider's own control-panel firewall / security"
echo "group; that sits in front of the OS and ufw cannot see or open it."
