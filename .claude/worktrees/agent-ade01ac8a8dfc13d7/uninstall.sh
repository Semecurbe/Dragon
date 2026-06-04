#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Drag and Rag — Script de désinstallation
#  Arrête le service, le supprime de systemd, et efface /opt/drag_and_rag.
#
#  Usage : sudo ./uninstall.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; NC='\033[0m'
ok()  { echo -e "${G}  ✔${NC}  $*"; }
warn(){ echo -e "${Y}  ⚠${NC}  $*"; }
die() { echo -e "${R}  ✖${NC}  $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Exécutez ce script en root : sudo ./uninstall.sh"

SERVICE_NAME="drag_and_rag"
DEST_DIR="/opt/drag_and_rag"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo ""
echo -e "${R}═══════════════════════════════════════════════════════${NC}"
echo -e "${R}   Drag and Rag — Désinstallation${NC}"
echo -e "${R}═══════════════════════════════════════════════════════${NC}"
echo ""
warn "Cette opération va :"
echo "    • Arrêter et supprimer le service systemd"
echo "    • Supprimer le répertoire $DEST_DIR"
echo ""
read -rp "  Confirmer la désinstallation ? [o/N] " CONFIRM
[[ "${CONFIRM,,}" =~ ^(o|oui|y|yes)$ ]] || { echo "Annulé."; exit 0; }
echo ""

# Arrêt et désactivation du service
if systemctl list-unit-files --quiet "${SERVICE_NAME}.service" &>/dev/null; then
    systemctl stop    "$SERVICE_NAME" 2>/dev/null && ok "Service arrêté"    || warn "Service déjà arrêté"
    systemctl disable "$SERVICE_NAME" 2>/dev/null && ok "Service désactivé" || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    ok "Fichier service supprimé : $SERVICE_FILE"
else
    warn "Service non trouvé dans systemd"
fi

# Suppression du répertoire d'installation
if [[ -d "$DEST_DIR" ]]; then
    rm -rf "$DEST_DIR"
    ok "Répertoire supprimé : $DEST_DIR"
else
    warn "Répertoire non trouvé : $DEST_DIR"
fi

echo ""
ok "Désinstallation terminée."
echo ""
