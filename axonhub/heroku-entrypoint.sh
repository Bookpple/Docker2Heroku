#!/bin/sh
set -eu
echo "[heroku-entrypoint] shell ready"
: "${PORT:?Heroku PORT is required}"
export AXONHUB_SERVER_PORT="$PORT"
export AXONHUB_SERVER_HOST="0.0.0.0"
printf '[heroku-entrypoint] launching AxonHub on port %s\n' "$PORT"
exec /app/axonhub "$@"
