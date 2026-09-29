#!/usr/bin/env bash
# Install ocjump and its companion OpenCode plugin for the current user.
#
# Idempotent: re-running refreshes the bin shim and the plugin symlink.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
PLUGIN_DIR="${HOME}/.config/opencode/plugins"
BIN_PATH="${BIN_DIR}/ocjump"
PLUGIN_PATH="${PLUGIN_DIR}/oc-state.js"

mkdir -p "${BIN_DIR}" "${PLUGIN_DIR}"

cat > "${BIN_PATH}" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${REPO}/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m ocjump "\$@"
EOF
chmod +x "${BIN_PATH}"

ln -sf "${REPO}/plugin/oc-state.js" "${PLUGIN_PATH}"

echo "Installed:"
echo "  - ${BIN_PATH} -> python -m ocjump (${REPO}/src)"
echo "  - ${PLUGIN_PATH} -> ${REPO}/plugin/oc-state.js"
echo
echo "Ensure ${BIN_DIR} is on your PATH, then add this to ~/.tmux.conf:"
echo
echo "    bind j display-popup -E -w 80% -h 75% 'ocjump'"
echo
echo "Restart OpenCode (plugins load at startup) and reload tmux (prefix + r)."
