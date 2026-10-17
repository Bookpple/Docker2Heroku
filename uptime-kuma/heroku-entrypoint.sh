#!/bin/sh
set -eu

fail() {
    printf '[heroku-entrypoint] ERROR: %s\n' "$1" >&2
    exit 1
}

: "${PORT:?Heroku PORT is required}"
: "${AIVEN_CA_BASE64:?AIVEN_CA_BASE64 is required}"
case "$PORT" in
    *[!0-9]*) fail 'PORT must be an integer' ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || fail 'PORT must be between 1 and 65535'

for db_key in UPTIME_KUMA_DB_HOSTNAME UPTIME_KUMA_DB_PORT UPTIME_KUMA_DB_NAME UPTIME_KUMA_DB_USERNAME UPTIME_KUMA_DB_PASSWORD; do
    [ -n "$(printenv "$db_key")" ] || fail "$db_key is required"
done
[ "${UPTIME_KUMA_DB_TYPE:-mariadb}" = mariadb ] || fail 'UPTIME_KUMA_DB_TYPE must be mariadb'

umask 077
export DATA_DIR=/tmp/uptime-kuma
mkdir -p "$DATA_DIR/upload"
ca_file="$DATA_DIR/aiven-ca.pem"
if ! printf '%s' "$AIVEN_CA_BASE64" | tr -d '[:space:]' | base64 -d > "$ca_file"; then
    fail 'AIVEN_CA_BASE64 must contain a Base64-encoded PEM certificate'
fi
if ! node -e 'new (require("node:crypto").X509Certificate)(require("node:fs").readFileSync(process.argv[1]))' "$ca_file" 2>/dev/null; then
    fail 'The decoded Aiven CA is not a valid X.509 certificate'
fi

export UPTIME_KUMA_HOST=0.0.0.0
export UPTIME_KUMA_PORT="$PORT"
export UPTIME_KUMA_DB_TYPE=mariadb
export UPTIME_KUMA_DB_SSL=true
export UPTIME_KUMA_DB_CA_FILE="$ca_file"
unset UPTIME_KUMA_DB_CA UPTIME_KUMA_DB_SSL_FILE UPTIME_KUMA_DB_SOCKET
unset UPTIME_KUMA_DB_USERNAME_FILE UPTIME_KUMA_DB_PASSWORD_FILE NODE_TLS_REJECT_UNAUTHORIZED

printf '[heroku-entrypoint] Starting Uptime Kuma on port %s with Aiven CA verification\n' "$PORT"
cd /app
exec /bin/sh /app/exec-command.sh "$@"
