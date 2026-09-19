#!/bin/sh
# Correct /data's ownership, then run the app unprivileged.
#
# Docker creates a missing bind-mount source directory as root, so a newcomer's
# ./data is root-owned before the container ever starts; an image that simply
# declared USER would fail to open the database on first boot. Starting as root
# and dropping privileges here works whatever the host did.
set -e

APP_UID=1000
APP_GID=1000

if [ "$(id -u)" != "0" ]; then
    # Started with an explicit `user:`. We cannot chown, and the operator has
    # taken ownership of the decision.
    exec "$@"
fi

if [ -d /data ] && [ "$(stat -c %u /data)" != "$APP_UID" ]; then
    # Compare the top-level owner first: the recursive walk is then a one-time
    # upgrade cost rather than something every boot pays over every stored PDF.
    if ! chown -R "$APP_UID:$APP_GID" /data; then
        echo "WARNING: could not chown /data (read-only or networked mount?)." >&2
        echo "WARNING: continuing as root; files under ./data will be root-owned." >&2
        exec "$@"
    fi
fi

exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$@"
