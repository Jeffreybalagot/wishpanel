#!/bin/sh
# Apply Unraid-style PUID/PGID/UMASK, then drop root and exec the app.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

umask "$UMASK"

if [ "$(id -u)" = "0" ]; then
    if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
        echo "[entrypoint] Refusing to run as root (PUID/PGID=0). Use e.g. PUID=99 PGID=100." >&2
        exit 1
    fi
    groupmod -o -g "$PGID" wishpanel
    usermod -o -u "$PUID" wishpanel
    mkdir -p "$CONFIG_DIR"
    # Only touch our own config tree, and only if ownership is actually wrong.
    if [ "$(stat -c %u:%g "$CONFIG_DIR")" != "$PUID:$PGID" ] \
       || find "$CONFIG_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit | grep -q .; then
        echo "[entrypoint] Setting ownership of $CONFIG_DIR to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$CONFIG_DIR"
    fi
    echo "[entrypoint] Starting WishPanel as uid=$PUID gid=$PGID umask=$UMASK"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

echo "[entrypoint] Starting WishPanel as uid=$(id -u) gid=$(id -g)"
exec "$@"
