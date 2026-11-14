#!/bin/sh
# Removes Niimbot Proxy. Shipped inside the app:
#   sudo /Applications/NiimbotProxy.app/Contents/Resources/uninstall.sh
# Per-user config (~/Library/Application Support/NiimbotProxy) and logs are left alone.
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
LABEL=local.niimbot-proxy
for uid in $(ps -axo uid=,comm= | awk '$2 ~ /loginwindow$/ {print $1}' | sort -u); do
  launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
done
pkill -x NiimbotProxy 2>/dev/null || true
lpadmin -x Niimbot_B1 2>/dev/null || true
rm -f "/Library/LaunchAgents/$LABEL.plist"
rm -rf /Library/Printers/NiimbotB1 /Applications/NiimbotProxy.app "/Library/Application Support/NiimbotProxy"
pkgutil --forget "$LABEL" >/dev/null 2>&1 || true
echo "Niimbot Proxy removed."
