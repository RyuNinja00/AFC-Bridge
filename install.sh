#!/usr/bin/env bash
# INDX Bridge (unofficial): link it into Kalico and INDX's extension folder.
#   ./install.sh          link, then check the install (tools/check_compat.py)
#   ./install.sh remove   unlink
# Needs the INDX Suite (INDX's install.sh, not --macros-only) and AFC first.
# KLIPPER_DIR= and CONFIG_DIR= override what it finds.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"

find_klipper() {
  # the running service's directory first, as INDX's installer does
  local d
  for svc in klipper kalico; do
    d="$(systemctl show -p WorkingDirectory --value "$svc" 2>/dev/null || true)"
    [ -n "$d" ] && [ -d "$d/klippy" ] && { echo "$d"; return; }
  done
  for d in "$HOME/klipper" "$HOME/kalico" "$HOME"/kalico-*; do
    [ -d "$d/klippy" ] && { echo "$d"; return; }
  done
}
KLIPPER_DIR="${KLIPPER_DIR:-$(find_klipper)}"
[ -n "$KLIPPER_DIR" ] && [ -d "$KLIPPER_DIR/klippy" ] || { echo "No Klipper/Kalico found. Run: KLIPPER_DIR=<its folder> $0"; exit 1; }

# where INDX's modules are is where this one goes (klippy/plugins on Kalico)
MOD_DIR=""
for d in "$KLIPPER_DIR/klippy/plugins" "$KLIPPER_DIR/klippy/extras"; do
  [ -e "$d/indx_api.py" ] && { MOD_DIR="$d"; break; }
done
CFG="$CONFIG_DIR/indx/ext/indx_bridge.cfg"

if [ "${1:-}" = "remove" ]; then
  for f in "$CFG" "$KLIPPER_DIR/klippy/plugins/indx_bridge.py" "$KLIPPER_DIR/klippy/extras/indx_bridge.py"; do
    [ -L "$f" ] && rm "$f" && echo "removed $f"
  done
  echo "Remove [indx_bridge] from printer.cfg, then restart the Klipper service."
  exit 0
fi

[ -n "$MOD_DIR" ] || { echo "INDX's indx_api.py is not in $KLIPPER_DIR/klippy/plugins or extras: install the INDX Suite first (INDX's install.sh, not --macros-only)"; exit 1; }
for m in indx_extension.py indx_remap.py; do
  [ -e "$MOD_DIR/$m" ] || { echo "INDX's $m is not in $MOD_DIR: update the INDX Suite"; exit 1; }
done
[ -e "$KLIPPER_DIR/klippy/extras/AFC.py" ] || [ -e "$KLIPPER_DIR/klippy/plugins/AFC.py" ] || { echo "AFC is not installed in $KLIPPER_DIR: install AFC first"; exit 1; }
grep -qs '^\[include ext/\*\.cfg\]' "$CONFIG_DIR/indx/indx_includes.cfg" || { echo "$CONFIG_DIR/indx/indx_includes.cfg does not include ext/*.cfg: update INDX first"; exit 1; }

mkdir -p "$CONFIG_DIR/indx/ext"
ln -sfn "$HERE/macros/indx_bridge.cfg" "$CFG" && echo "linked $CFG"
ln -sfn "$HERE/klipper_extras/indx_bridge.py" "$MOD_DIR/indx_bridge.py" && echo "linked $MOD_DIR/indx_bridge.py"
# moving from indx-afc (0.2.x): its links go, [indx_afc] in your config
# becomes [indx_bridge] (a backup of each file it changes is kept)
for f in "$CONFIG_DIR/indx/ext/indx_afc.cfg" "$KLIPPER_DIR/klippy/plugins/indx_afc.py" "$KLIPPER_DIR/klippy/extras/indx_afc.py"; do
  [ -L "$f" ] && rm "$f" && echo "removed the old indx-afc link $f"
done
while IFS= read -r f; do
  [ -n "$f" ] || continue
  cp -p "$f" "$f.before-indx-bridge" && sed -i 's/^\[indx_afc\]/[indx_bridge]/' "$f" \
    && echo "renamed [indx_afc] to [indx_bridge] in $f (the old file: $f.before-indx-bridge)"
done < <(grep -rlE '^\[indx_afc\]' "$CONFIG_DIR" --include='*.cfg' 2>/dev/null || true)
OLD_CMDS="$(grep -rlE 'INDX_AFC_' "$CONFIG_DIR" --include='*.cfg' 2>/dev/null | grep -v '/indx/' || true)"
if [ -n "$OLD_CMDS" ]; then
  echo "These files still use the old INDX_AFC_* commands, which are gone (now INDX_BRIDGE_*):"
  echo "$OLD_CMDS" | sed 's/^/  /'
fi
# keep the link out of Kalico's git status
EXCL="$KLIPPER_DIR/.git/info/exclude"
[ -f "$EXCL" ] && ! grep -qs 'indx_bridge.py' "$EXCL" && echo "klippy/*/indx_bridge.py" >> "$EXCL"

echo
python3 "$HERE/tools/check_compat.py" --klipper "$KLIPPER_DIR" --config "$CONFIG_DIR" || true
cat <<EOT

Now in printer.cfg (docs/indx-bridge.md has the AFC settings it needs):
  [indx_bridge]
  lanes: lane1, lane2, lane3, lane4
Then restart the Klipper service (a plain RESTART does not load a new module),
and run INDX_BRIDGE_STATUS.
EOT
