#!/usr/bin/env bash
# Uninstall ocjump and its companion OpenCode plugin for the current user.
#
# Removes exactly what install.sh created (the bin shim and the plugin
# symlink). Idempotent: missing files are reported, not fatal. The user's
# config (~/.config/ocjump/config.ini) and the repository itself are left
# untouched.
set -euo pipefail

BIN_PATH="${HOME}/.local/bin/ocjump"
PLUGIN_PATH="${HOME}/.config/opencode/plugins/oc-state.js"

removed=0

if [[ -e "${BIN_PATH}" ]]; then
  rm -f "${BIN_PATH}"
  echo "removed ${BIN_PATH}"
  removed=1
fi

# Only remove the plugin symlink if it still points into an ocjump checkout;
# a real file dropped there by the user is left alone.
if [[ -L "${PLUGIN_PATH}" ]]; then
  rm -f "${PLUGIN_PATH}"
  echo "removed ${PLUGIN_PATH}"
  removed=1
elif [[ -e "${PLUGIN_PATH}" ]]; then
  echo "kept ${PLUGIN_PATH} (not a symlink; remove it manually if it is ours)"
fi

if [[ "${removed}" -eq 0 ]]; then
  echo "nothing to remove (ocjump does not appear to be installed)"
fi

echo
echo "Left untouched:"
echo "  - ${HOME}/.config/ocjump/config.ini (your configuration, if any)"
echo "  - the repository checkout"
echo
echo "Remember to drop the 'bind j display-popup ...' line from ~/.tmux.conf,"
echo "reload tmux (prefix + r), and restart OpenCode so the plugin stops loading."
