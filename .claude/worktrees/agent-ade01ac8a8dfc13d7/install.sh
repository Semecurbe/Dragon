#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Drag and Rag — Script d'installation système
#  Copie le projet dans /opt/drag_and_rag et configure le service systemd.
#
#  Usage :  sudo ./install.sh
#  Mise à jour (réinstall) : sudo ./install.sh  — idempotent, relance le service
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Couleurs ──────────────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${B}  →${NC}  $*"; }
ok()      { echo -e "${G}  ✔${NC}  $*"; }
warn()    { echo -e "${Y}  ⚠${NC}  $*"; }
die()     { echo -e "${R}  ✖${NC}  $*" >&2; exit 1; }
title()   { echo -e "\n${B}${*}${NC}"; }

# ── Vérifications préalables ──────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Exécutez ce script en root : sudo ./install.sh"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="/opt/drag_and_rag"
SERVICE_NAME="drag_and_rag"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Détecter l'utilisateur qui a lancé sudo (ou le premier utilisateur non-root)
if [[ -n "${SUDO_USER:-}" ]]; then
    APP_USER="$SUDO_USER"
elif [[ -n "${LOGNAME:-}" ]] && [[ "$LOGNAME" != "root" ]]; then
    APP_USER="$LOGNAME"
else
    APP_USER=$(getent passwd | awk -F: '$3>=1000 && $3<65534 {print $1; exit}')
fi
[[ -n "$APP_USER" ]] || die "Impossible de déterminer l'utilisateur applicatif."
APP_GROUP=$(id -gn "$APP_USER")
APP_HOME=$(eval echo "~$APP_USER")

PYTHON_BIN=$(su -s /bin/bash -c "which python3" "$APP_USER" 2>/dev/null \
             || which python3 \
             || die "python3 introuvable.")
VENV_BIN="$DEST_DIR/env/bin"
APP_PORT=7860

# ── Résumé ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${B}═══════════════════════════════════════════════════════${NC}"
echo -e "${B}   Drag and Rag — Installation                         ${NC}"
echo -e "${B}═══════════════════════════════════════════════════════${NC}"
info "Source        : $SRC_DIR"
info "Destination   : $DEST_DIR"
info "Utilisateur   : $APP_USER ($APP_GROUP)"
info "Python        : $PYTHON_BIN  ($(python3 --version 2>&1))"
info "Port Flask    : $APP_PORT"
info "Service       : $SERVICE_FILE"
echo ""

# Confirmation si ce n'est pas une mise à jour silencieuse
if [[ ! -d "$DEST_DIR" ]]; then
    read -rp "  Continuer ? [O/n] " CONFIRM
    [[ "${CONFIRM,,}" =~ ^(o|oui|y|yes|)$ ]] || { echo "Annulé."; exit 0; }
fi

# ── 1. Copie des fichiers ─────────────────────────────────────────────────────
title "1/6  Copie des fichiers vers $DEST_DIR …"
mkdir -p "$DEST_DIR"

if command -v rsync &>/dev/null; then
    rsync -a --delete --info=progress2 \
        --exclude='__pycache__/'   \
        --exclude='*.pyc'          \
        --exclude='.pyc'           \
        --exclude='env/'           \
        --exclude='.git/'          \
        "$SRC_DIR/" "$DEST_DIR/"
else
    # Fallback sans rsync
    cp -r "$SRC_DIR/." "$DEST_DIR/"
    rm -rf "$DEST_DIR/__pycache__" "$DEST_DIR/env" "$DEST_DIR/.git" 2>/dev/null || true
    find "$DEST_DIR" -name "*.pyc" -delete 2>/dev/null || true
fi

chown -R "$APP_USER:$APP_GROUP" "$DEST_DIR"
ok "Fichiers copiés — propriétaire : $APP_USER"

# ── 2. Environnement virtuel Python ──────────────────────────────────────────
title "2/6  Environnement virtuel Python …"

if [[ -d "$DEST_DIR/env" ]]; then
    warn "Environnement existant détecté — mise à jour uniquement."
    su -s /bin/bash "$APP_USER" -c \
        "$DEST_DIR/env/bin/pip install --quiet --no-cache-dir --upgrade pip"
else
    su -s /bin/bash "$APP_USER" -c \
        "$PYTHON_BIN -m venv '$DEST_DIR/env'"
    ok "Environnement virtuel créé : $DEST_DIR/env"
fi

# ── 3. Installation des dépendances ──────────────────────────────────────────
title "3/6  Installation des dépendances Python …"
info "(Cela peut prendre plusieurs minutes lors de la première installation)"

# Vider le cache pip corrompu avant d'installer (évite les warnings
# "Cache entry deserialization failed" liés à des entrées d'une autre version)
su -s /bin/bash "$APP_USER" -c \
    "$VENV_BIN/pip cache purge" 2>/dev/null || true

su -s /bin/bash "$APP_USER" -c \
    "$VENV_BIN/pip install --quiet --no-cache-dir --upgrade pip"
su -s /bin/bash "$APP_USER" -c \
    "$VENV_BIN/pip install --quiet --no-cache-dir -r '$DEST_DIR/requirements.txt'"
ok "Dépendances installées"

# ── 4. Mise à jour de .rag_config.json ───────────────────────────────────────
title "4/6  Configuration …"

CONFIG="$DEST_DIR/.rag_config.json"
if [[ -f "$CONFIG" ]]; then
    # Remplace l'ancien chemin source par le nouveau chemin d'installation
    su -s /bin/bash "$APP_USER" -c "
$VENV_BIN/python3 - <<'PYEOF'
import json, pathlib, sys

config_path = pathlib.Path('$CONFIG')
config = json.loads(config_path.read_text())

old_dir = '$SRC_DIR'
new_dir = '$DEST_DIR'

for key, val in config.items():
    if isinstance(val, str) and old_dir in val:
        config[key] = val.replace(old_dir, new_dir)
        print(f'  Mis à jour : {key}')

# Retirer la clé API du fichier (sera re-saisie dans l'interface)
# config.pop('anthropic_api_key', None)

config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
PYEOF
"
    ok ".rag_config.json mis à jour (chemins: $SRC_DIR → $DEST_DIR)"
else
    echo '{}' > "$CONFIG"
    chown "$APP_USER:$APP_GROUP" "$CONFIG"
    ok ".rag_config.json vide créé (la clé API sera saisie au premier lancement)"
fi

# ── 5. Création du service systemd ───────────────────────────────────────────
title "5/6  Service systemd : $SERVICE_NAME …"

# Arrêter le service existant avant de le remplacer
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    warn "Service arrêté pour mise à jour"
fi

cat > "$SERVICE_FILE" << EOF
# ═════════════════════════════════════════════════════════════════
#  Drag and Rag — Service systemd
#  Fichier généré par install.sh — modifiez avec prudence.
#
#  Commandes utiles :
#    sudo systemctl status  $SERVICE_NAME
#    sudo systemctl restart $SERVICE_NAME
#    sudo systemctl stop    $SERVICE_NAME
#    sudo journalctl -u     $SERVICE_NAME -f
# ═════════════════════════════════════════════════════════════════

[Unit]
Description=Drag and Rag — Application RAG locale (Flask)
Documentation=file://${DEST_DIR}/README.md
After=network.target
Wants=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${DEST_DIR}

# Interpréteur Python du virtualenv
ExecStart=${VENV_BIN}/python3 ${DEST_DIR}/app_flask.py

# Variables d'environnement
Environment="HOME=${APP_HOME}"
Environment="PATH=${VENV_BIN}:/usr/local/bin:/usr/bin:/bin"
# Cache des modèles dans le répertoire de l'app (évite les re-téléchargements)
Environment="HF_HOME=${DEST_DIR}/.cache/huggingface"
Environment="TRANSFORMERS_CACHE=${DEST_DIR}/.cache/transformers"
Environment="XDG_CACHE_HOME=${DEST_DIR}/.cache"

# Redémarrage automatique en cas d'erreur
Restart=on-failure
RestartSec=10s

# Logs vers journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drag_and_rag

# Sécurité minimale (sans bloquer les accès nécessaires)
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

ok "Service créé : $SERVICE_FILE"

# ── 6. Activation et démarrage ────────────────────────────────────────────────
title "6/6  Activation du service …"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start  "$SERVICE_NAME"

# Attendre 3 secondes puis vérifier l'état
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service démarré avec succès"
else
    warn "Le service ne semble pas démarré — vérifiez les logs :"
    echo "      sudo journalctl -u $SERVICE_NAME -n 30"
fi

# ── Résumé final ──────────────────────────────────────────────────────────────
echo ""
echo -e "${G}═══════════════════════════════════════════════════════${NC}"
echo -e "${G}   Installation terminée !${NC}"
echo -e "${G}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Application :  ${B}http://localhost:${APP_PORT}${NC}"
echo ""
echo "  Commandes utiles :"
printf "    %-45s %s\n" "sudo systemctl status  $SERVICE_NAME"  "# état"
printf "    %-45s %s\n" "sudo systemctl restart $SERVICE_NAME"  "# relancer"
printf "    %-45s %s\n" "sudo systemctl stop    $SERVICE_NAME"  "# arrêter"
printf "    %-45s %s\n" "sudo systemctl disable $SERVICE_NAME"  "# désactiver au démarrage"
printf "    %-45s %s\n" "sudo journalctl -u $SERVICE_NAME -f"   "# logs en direct"
echo ""
echo -e "  Pour mettre à jour l'application :"
echo -e "    cd $SRC_DIR && sudo ./install.sh"
echo ""
