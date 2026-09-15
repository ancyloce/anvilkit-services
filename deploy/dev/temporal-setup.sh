#!/bin/sh
# Temporal schema and namespace setup with the official admin tools (C02).
# The namespace is `anvilkit` (architecture.md naming); no legacy history is
# imported into it.
set -eu
case "$1" in
  schemas)
    for store in temporal visibility; do
      SQL_DATABASE=temporal
      if [ "$store" = visibility ]; then SQL_DATABASE=temporal_visibility; fi
      export SQL_DATABASE
      temporal-sql-tool setup-schema -v 0.0
      temporal-sql-tool update-schema -d "/etc/temporal/schema/postgresql/v12/$store/versioned"
    done
    ;;
  namespace)
    cli() { temporal "$@" --address temporal:7233 --command-timeout 5s; }
    attempts=0
    until cli operator cluster health >/dev/null 2>&1; do
      attempts=$((attempts + 1))
      if [ "$attempts" -ge 60 ]; then echo "temporal frontend not healthy" >&2; exit 1; fi
      sleep 1
    done
    if ! cli operator namespace describe --namespace anvilkit >/dev/null 2>&1; then
      cli operator namespace create --namespace anvilkit --retention 24h
    fi
    cli operator namespace describe --namespace anvilkit
    ;;
  *) exit 2 ;;
esac
