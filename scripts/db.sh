#!/usr/bin/env bash
# Convenience wrapper around the Docker stack and the SQL migrations.
#
#   ./scripts/db.sh up      start postgres + redis + mongo, wait for health
#   ./scripts/db.sh apply   run sql/01..05 + 07 (schema, triggers, indexes, views)
#   ./scripts/db.sh seed    run sql/06_seed.sql
#   ./scripts/db.sh reset   apply + seed
#   ./scripts/db.sh psql    interactive shell (extra args are passed through)
#   ./scripts/db.sh redis   redis-cli shell
#   ./scripts/db.sh mongo   mongosh shell (restaurant_feedback)
#   ./scripts/db.sh down    stop containers, keep volumes
#   ./scripts/db.sh nuke    stop containers and delete volumes
#
# psql runs *inside* the container, so no local client is required and there is
# no chance of a client/server version mismatch.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DB_USER=rr_app
DB_NAME=restaurant_reservations
# 06 is the seed, not schema, so it is applied separately by `seed`.
SCHEMA_FILES=(01_extensions 02_tables 03_triggers 04_indexes 05_views 07_analytics)

compose() { docker compose "$@"; }

run_sql() {
    compose exec -T postgres \
        psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -f "/sql/$1.sql"
}

require_up() {
    if ! compose exec -T postgres pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
        echo "postgres is not ready — run './scripts/db.sh up' first" >&2
        exit 1
    fi
}

cmd_up() {
    compose up -d
    echo -n "waiting for postgres"
    for _ in $(seq 1 40); do
        if compose exec -T postgres pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
            echo " ready"
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo " timed out" >&2
    compose logs --tail=40 postgres >&2
    exit 1
}

cmd_apply() {
    require_up
    for f in "${SCHEMA_FILES[@]}"; do
        echo "==> $f.sql"
        run_sql "$f"
    done
}

cmd_seed() {
    require_up
    echo "==> 06_seed.sql"
    run_sql 06_seed
}

case "${1:-}" in
    up)    cmd_up ;;
    apply) cmd_apply ;;
    seed)  cmd_seed ;;
    reset) cmd_apply; cmd_seed ;;
    psql)  shift; compose exec postgres psql -U "$DB_USER" -d "$DB_NAME" "$@" ;;
    redis) shift; compose exec redis redis-cli "$@" ;;
    mongo) shift; compose exec mongo mongosh \
               -u "$DB_USER" -p rr_password --authenticationDatabase admin \
               restaurant_feedback "$@" ;;
    down)  compose down ;;
    nuke)  compose down -v ;;
    # Prints this file's own header block, so help can never drift from docs.
    *)     sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac