#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Dragon — System installation script
#  Copies the project to /opt/dragon and configures the systemd service.
#
#  Usage:   sudo ./install.sh        (recommended)
#           sudo bash install.sh     (equivalent)
# ═══════════════════════════════════════════════════════════════════════════════

# Re-execute under bash if invoked with sh/dash (e.g. "sh install.sh")
[ -n "$BASH_VERSION" ] || exec bash "$0" "$@"

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${B}  →${NC}  $*"; }
ok()    { echo -e "${G}  ✔${NC}  $*"; }
warn()  { echo -e "${Y}  ⚠${NC}  $*"; }
die()   { echo -e "${R}  ✖${NC}  $*" >&2; exit 1; }
title() { echo -e "\n${B}${*}${NC}"; }

# ── Pre-flight checks ─────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run this script as root: sudo ./install.sh"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="/opt/dragon"
SERVICE_NAME="dragon"
OLD_SERVICE_NAME="drag_and_rag"          # legacy — will be stopped/disabled
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_DIR="/var/log/dragon"
LOGROTATE_FILE="/etc/logrotate.d/dragon"

# Detect the non-root user who invoked sudo
if [[ -n "${SUDO_USER:-}" ]]; then
    APP_USER="$SUDO_USER"
elif [[ -n "${LOGNAME:-}" ]] && [[ "$LOGNAME" != "root" ]]; then
    APP_USER="$LOGNAME"
else
    APP_USER=$(getent passwd | awk -F: '$3>=1000 && $3<65534 {print $1; exit}')
fi
[[ -n "$APP_USER" ]] || die "Cannot determine the application user."
APP_GROUP=$(id -gn "$APP_USER")
APP_HOME=$(eval echo "~$APP_USER")

PYTHON_BIN=$(su -s /bin/bash -c "which python3" "$APP_USER" 2>/dev/null \
             || which python3 \
             || die "python3 not found.")
VENV_BIN="$DEST_DIR/env/bin"
APP_PORT=7860

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${B}═══════════════════════════════════════════════════════${NC}"
echo -e "${B}   Dragon — Installation                               ${NC}"
echo -e "${B}═══════════════════════════════════════════════════════${NC}"
info "Source      : $SRC_DIR"
info "Destination : $DEST_DIR"
info "User        : $APP_USER ($APP_GROUP)"
info "Python      : $PYTHON_BIN  ($(python3 --version 2>&1))"
info "Flask port  : $APP_PORT"
info "Service     : $SERVICE_FILE"
info "Logs        : $LOG_DIR/"
echo ""

if [[ ! -d "$DEST_DIR" ]]; then
    read -rp "  Continue? [Y/n] " CONFIRM
    [[ "${CONFIRM,,}" =~ ^(y|yes|o|oui|)$ ]] || { echo "Cancelled."; exit 0; }
fi

# ── 0. Legacy service migration ───────────────────────────────────────────────
if systemctl list-unit-files --quiet "${OLD_SERVICE_NAME}.service" &>/dev/null \
   && systemctl is-enabled --quiet "${OLD_SERVICE_NAME}" 2>/dev/null; then
    warn "Legacy service '${OLD_SERVICE_NAME}' found — stopping and disabling it."
    systemctl stop    "${OLD_SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${OLD_SERVICE_NAME}" 2>/dev/null || true
    ok "Legacy service disabled"
fi

# ── 1. Copy files ─────────────────────────────────────────────────────────────
title "1/7  Copying files to $DEST_DIR …"
mkdir -p "$DEST_DIR"

if command -v rsync &>/dev/null; then
    rsync -a --delete --info=progress2 \
        --exclude='__pycache__/'   \
        --exclude='*.pyc'          \
        --exclude='env/'           \
        --exclude='.git/'          \
        "$SRC_DIR/" "$DEST_DIR/"
else
    cp -r "$SRC_DIR/." "$DEST_DIR/"
    rm -rf "$DEST_DIR/__pycache__" "$DEST_DIR/env" "$DEST_DIR/.git" 2>/dev/null || true
    find "$DEST_DIR" -name "*.pyc" -delete 2>/dev/null || true
fi

chown -R "$APP_USER:$APP_GROUP" "$DEST_DIR"
ok "Files copied — owner: $APP_USER"

# ── 2. Python virtual environment ─────────────────────────────────────────────
title "2/7  Python virtual environment …"

if [[ -d "$DEST_DIR/env" ]]; then
    warn "Existing environment detected — upgrading pip only."
    su -s /bin/bash "$APP_USER" -c \
        "$DEST_DIR/env/bin/pip install --quiet --no-cache-dir --upgrade pip"
else
    su -s /bin/bash "$APP_USER" -c \
        "$PYTHON_BIN -m venv '$DEST_DIR/env'"
    ok "Virtual environment created: $DEST_DIR/env"
fi

# ── 3. Python dependencies ────────────────────────────────────────────────────
title "3/7  Installing Python dependencies …"
info "(This may take several minutes on first install)"

su -s /bin/bash "$APP_USER" -c \
    "$VENV_BIN/pip cache purge" 2>/dev/null || true
su -s /bin/bash "$APP_USER" -c \
    "$VENV_BIN/pip install --quiet --no-cache-dir --upgrade pip"
su -s /bin/bash "$APP_USER" -c \
    "$VENV_BIN/pip install --quiet --no-cache-dir -r '$DEST_DIR/requirements.txt'"
ok "Dependencies installed"

# ── 4. Update .rag_config.json paths ─────────────────────────────────────────
title "4/7  Updating configuration …"

CONFIG="$DEST_DIR/.rag_config.json"
if [[ -f "$CONFIG" ]]; then
    su -s /bin/bash "$APP_USER" -c "
$VENV_BIN/python3 - <<'PYEOF'
import json, pathlib

config_path = pathlib.Path('$CONFIG')
config = json.loads(config_path.read_text())

for src_dir in ('$SRC_DIR', '/opt/drag_and_rag'):
    for key, val in config.items():
        if isinstance(val, str) and src_dir in val:
            config[key] = val.replace(src_dir, '$DEST_DIR')
            print(f'  Updated: {key}')

config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
PYEOF
"
    ok ".rag_config.json updated (paths → $DEST_DIR)"
else
    echo '{}' > "$CONFIG"
    chown "$APP_USER:$APP_GROUP" "$CONFIG"
    ok ".rag_config.json created (API key will be entered on first launch)"
fi

# ── 5. Log directory ──────────────────────────────────────────────────────────
title "5/7  Log directory $LOG_DIR …"

mkdir -p "$LOG_DIR"
chown "$APP_USER:$APP_GROUP" "$LOG_DIR"
chmod 750 "$LOG_DIR"
ok "Log directory ready: $LOG_DIR"

# Logrotate configuration
cat > "$LOGROTATE_FILE" << EOF
$LOG_DIR/*.log
$LOG_DIR/*.err {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 640 ${APP_USER} ${APP_GROUP}
}
EOF
ok "Logrotate configured: $LOGROTATE_FILE"

# ── 6. Systemd service ────────────────────────────────────────────────────────
title "6/7  Systemd service: $SERVICE_NAME …"

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    warn "Service stopped for update"
fi

cat > "$SERVICE_FILE" << EOF
# ═════════════════════════════════════════════════════════════════
#  Dragon — systemd service
#  Generated by install.sh — edit with care.
#
#  Useful commands:
#    sudo systemctl status  $SERVICE_NAME
#    sudo systemctl restart $SERVICE_NAME
#    sudo systemctl stop    $SERVICE_NAME
#    sudo journalctl -u     $SERVICE_NAME -f
#    tail -f $LOG_DIR/dragon.log
# ═════════════════════════════════════════════════════════════════

[Unit]
Description=Dragon — Local RAG application (Flask)
Documentation=file://${DEST_DIR}/INSTALL.md
After=network.target
Wants=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${DEST_DIR}

# Python interpreter from the virtualenv
ExecStart=${VENV_BIN}/python3 ${DEST_DIR}/app_flask.py

# Environment
Environment="HOME=${APP_HOME}"
Environment="PATH=${VENV_BIN}:/usr/local/bin:/usr/bin:/bin"
Environment="HF_HOME=${DEST_DIR}/.cache/huggingface"
Environment="TRANSFORMERS_CACHE=${DEST_DIR}/.cache/transformers"
Environment="XDG_CACHE_HOME=${DEST_DIR}/.cache"

# Auto-restart on failure
Restart=on-failure
RestartSec=10s

# Logging to /var/log/dragon/
StandardOutput=append:${LOG_DIR}/dragon.log
StandardError=append:${LOG_DIR}/dragon.err
SyslogIdentifier=dragon

# Basic hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

ok "Service file written: $SERVICE_FILE"

# ── 7. Enable and start ───────────────────────────────────────────────────────
title "7/7  Enabling and starting the service …"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start  "$SERVICE_NAME"

sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service started successfully"
else
    warn "Service does not appear to be running — check the logs:"
    echo "      sudo journalctl -u $SERVICE_NAME -n 30"
    echo "      tail -40 $LOG_DIR/dragon.err"
fi

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${G}═══════════════════════════════════════════════════════${NC}"
echo -e "${G}   Installation complete!${NC}"
echo -e "${G}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Application :  ${B}http://localhost:${APP_PORT}${NC}"
echo ""
echo "  Service commands:"
printf "    %-50s %s\n" "sudo systemctl status  $SERVICE_NAME"   "# status"
printf "    %-50s %s\n" "sudo systemctl restart $SERVICE_NAME"   "# restart"
printf "    %-50s %s\n" "sudo systemctl stop    $SERVICE_NAME"   "# stop"
printf "    %-50s %s\n" "sudo systemctl disable $SERVICE_NAME"   "# disable auto-start"
echo ""
echo "  Logs:"
printf "    %-50s %s\n" "tail -f $LOG_DIR/dragon.log"            "# application output"
printf "    %-50s %s\n" "tail -f $LOG_DIR/dragon.err"            "# errors"
printf "    %-50s %s\n" "sudo journalctl -u $SERVICE_NAME -f"    "# systemd journal"
echo ""
echo -e "  To update the application:"
echo -e "    cd $SRC_DIR && sudo ./install.sh"
echo ""
