#!/bin/sh
set -eu

case "$1" in
    schemas)
        for store in temporal visibility; do
            SQL_DATABASE=temporal
            if [ "$store" = visibility ]; then SQL_DATABASE=temporal_visibility; fi
            export SQL_DATABASE
            # setup-schema preserves a version newer than 0.0. update-schema
            # resumes the retained version; neither command drops the database.
            temporal-sql-tool setup-schema -v 0.0
            temporal-sql-tool update-schema -d "/etc/temporal/schema/postgresql/v12/$store/versioned"
        done
        ;;
    namespace)
        cli() {
            temporal "$@" --address 127.0.0.1:17233 --namespace anvilkit-local-check \
                --tls --tls-server-name temporal.local --tls-ca-path /run/local/ca.crt \
                --tls-cert-path /run/local/client.crt --tls-key-path /run/local/client.key \
                --command-timeout 5s
        }
        attempts=0
        until cli operator cluster health >/dev/null 2>&1; do
            attempts=$((attempts + 1))
            if [ "$attempts" -ge 30 ]; then exit 1; fi
            sleep 1
        done
        if ! cli operator namespace describe >/dev/null 2>&1; then
            cli operator namespace create --retention 24h
        fi
        cli operator namespace describe
        ;;
    *) exit 2 ;;
esac
