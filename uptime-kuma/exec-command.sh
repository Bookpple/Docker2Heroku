#!/bin/sh
set -eu

if [ "$#" -eq 0 ] || [ -z "${1:-}" ]; then
    printf '[heroku-entrypoint] ERROR: A startup command is required\n' >&2
    exit 64
fi

if [ "$#" -eq 1 ]; then
    # Heroku supplies CMD as one shell command, e.g. "node server/server.js".
    # Only the startup command is shell code; config values stay in the environment.
    exec /bin/sh -c "exec $1"
fi

# Docker supplies the executable and each argument separately; keep them literal.
exec "$@"
