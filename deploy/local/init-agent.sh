#!/bin/sh
set -eu

# The PostgreSQL image runs this only for an empty data volume. Each canonical
# Agent migration is a separate transaction; runtime services execute no DDL.
psql -X -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 --single-transaction -f /contracts/roles-v1.sql
psql -X -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -f /run/bootstrap/databases.sql
for schema in activation control; do
    psql -X -U "$POSTGRES_USER" -d anvilkit_agent -v ON_ERROR_STOP=1 --single-transaction -f "/contracts/$schema-v1.sql"
done
