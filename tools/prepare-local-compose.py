#!/usr/bin/env python3
"""Prepare private configuration for the fixed Linux Compose development scope.

Reuses database passwords and caller tokens on subsequent runs. Certificates
and fixture authorization expire after 30 days; stop Compose before refreshing.
This command writes local files only and never starts or resets a database.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local/compose"


def write(path, content, mode=0o600):
    if path.exists():
        path.chmod(0o600)
    path.write_text(content)
    path.chmod(mode)


def document(path, value, mode=0o600):
    write(path, json.dumps(value, indent=2) + "\n", mode)


def environment(name, values):
    write(STATE / (name + ".env"), "".join(f"{key}={value}\n" for key, value in values.items()))


def certificate(authority, name, usage, destination, prefix, server=False):
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=authority, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", name + ".key", "-out", name + ".csr", "-subj", "/CN=" + name)
    san = "DNS:" + name + (",DNS:localhost,IP:127.0.0.1" if server else "")
    write(authority / (name + ".ext"), "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\n"
          + f"extendedKeyUsage={usage}\nsubjectAltName={san}\n")
    openssl("x509", "-req", "-in", name + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", name + ".crt", "-days", "30", "-extfile", name + ".ext")
    for suffix in ("crt", "key"):
        path = destination / (prefix + "." + suffix)
        # The host's enclosing STATE directory is private. Each non-root
        # container mounts only its own read-only configuration directory.
        write(path, (authority / (name + "." + suffix)).read_text(), 0o444)


def main():
    if not shutil.which("openssl"):
        raise SystemExit("OpenSSL is required to prepare the selected local mTLS identities")
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    secret_path = STATE / "secrets.json"
    # "workflow" (S2, 2026-09-13) is the Workflow service credential Control maps
    # to the preparation round methods; an older secrets file gains it in place.
    secret_names = ("postgres", "temporal", "migrator", "control", "api", "validation", "caller_a", "caller_b", "workflow")
    retained = json.loads(secret_path.read_text()) if secret_path.exists() else {}
    missing = [name for name in secret_names if name not in retained]
    for name in missing:
        retained[name] = secrets.token_hex(32)
    if missing or not secret_path.exists():
        document(secret_path, retained)
    # All interpolated secret values are generated hexadecimal strings.
    if any(len(retained.get(name, "")) != 64 or any(c not in "0123456789abcdef" for c in retained[name])
           for name in secret_names):
        raise SystemExit("The retained local credentials are invalid; no database credentials were replaced")

    for name in ("postgres", "api", "control", "workflow", "temporal", "admin"):
        (STATE / name).mkdir(exist_ok=True)
        (STATE / name).chmod(0o755)

    authorities = {}
    for name in ("api-control", "temporal"):
        directory = STATE / "authorities" / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", str(directory / "ca.key"), "-out", str(directory / "ca.crt"),
                        "-days", "30", "-subj", "/CN=anvilkit-compose-" + name],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        authorities[name] = directory
    certificate(authorities["api-control"], "anvilkit-agent-api", "clientAuth", STATE / "api", "client")
    certificate(authorities["api-control"], "anvilkit-agent-control", "serverAuth", STATE / "control", "server", True)
    # The Worker's Control client identity (S2): the preparation round methods.
    certificate(authorities["api-control"], "anvilkit-agent-workflow", "clientAuth", STATE / "workflow", "control-client")
    certificate(authorities["temporal"], "temporal.local", "serverAuth,clientAuth", STATE / "temporal", "server", True)
    for name in ("control", "workflow", "admin"):
        prefix = "temporal-client" if name == "control" else "client"
        certificate(authorities["temporal"], "anvilkit-agent-" + name, "clientAuth", STATE / name, prefix)
    for authority, targets in (
        ("api-control", (("api", "control-ca.crt"), ("control", "api-ca.crt"), ("workflow", "control-ca.crt"))),
        ("temporal", (("temporal", "ca.crt"), ("control", "temporal-ca.crt"), ("workflow", "ca.crt"), ("admin", "ca.crt"))),
    ):
        for directory, filename in targets:
            target = STATE / directory / filename
            write(target, (authorities[authority] / "ca.crt").read_text(), 0o444)

    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    identities = [{"token": retained["caller_" + suffix], "actorId": "fixture-actor-" + suffix,
                   "tenantId": "fixture-tenant-" + suffix} for suffix in ("a", "b")]
    actions = ["local-check.create", "operation.read", "operation.cancel", "component.prepare"]
    resource_kinds = {"local-check.create": "local-check", "component.prepare": "preparation"}
    document(STATE / "api/identities.json", {"identities": [dict(identity, grantedActions=actions + ["definition.validate"])
                                                          for identity in identities]}, 0o444)
    profile = {
        "schemaVersion": 1, "profile": "control-03-fixture", "sourceCredentialExpiresAt": expiry,
        "identities": [dict(identity, expiresAt=expiry) for identity in identities],
        "rolePermissions": [{"role": "fixture-developer", "resourceKind": resource_kinds.get(action, "operation"),
                             "action": action} for action in actions],
        "operationBindings": [{"operationId": "fixture-compose-" + suffix, "actorId": identity["actorId"],
                               "tenantId": identity["tenantId"], "serviceIdentity": "anvilkit-agent-api",
                               "serviceAuthorized": True, "validUntil": expiry}
                              for suffix, identity in zip(("a", "b"), identities)],
        "localCheckBindings": [{"actorId": identity["actorId"], "tenantId": identity["tenantId"],
                                "serviceIdentity": "anvilkit-agent-api", "serviceAuthorized": True, "validUntil": expiry}
                               for identity in identities],
    }
    document(STATE / "control/profile.json", profile, 0o444)
    document(STATE / "control/evidence.json", {"schemaVersion": 1, "tenants": [
        {"tenantId": identity["tenantId"], "complete": True, "available": True, "validUntil": expiry,
         "memberRoles": {identity["actorId"]: ["fixture-developer"]}} for identity in identities]}, 0o444)
    write(STATE / "caller-a.headers", "Authorization: Bearer " + retained["caller_a"] + "\n")

    bootstrap = []
    for name, group in (("migrator", "anvilkit_control_migrator"), ("control", "anvilkit_control_rw"), ("api", "anvilkit_api_ro")):
        bootstrap.append(f"CREATE ROLE anvilkit_local_{name} LOGIN NOBYPASSRLS PASSWORD '{retained[name]}' IN ROLE {group};")
    bootstrap.extend([
        f"CREATE ROLE anvilkit_temporal LOGIN NOBYPASSRLS PASSWORD '{retained['temporal']}';",
        "CREATE DATABASE anvilkit_agent OWNER anvilkit_control_migrator;",
        "CREATE DATABASE temporal OWNER anvilkit_temporal;",
        "CREATE DATABASE temporal_visibility OWNER anvilkit_temporal;",
        "REVOKE CONNECT ON DATABASE anvilkit_agent, temporal, temporal_visibility FROM PUBLIC;",
        "GRANT CONNECT ON DATABASE anvilkit_agent TO anvilkit_local_migrator, anvilkit_local_control, anvilkit_local_api;",
        "GRANT CONNECT ON DATABASE temporal, temporal_visibility TO anvilkit_temporal;",
    ])
    write(STATE / "postgres/databases.sql", "\n".join(bootstrap) + "\n", 0o444)
    environment("postgres", {"POSTGRES_USER": "postgres", "POSTGRES_DB": "postgres", "POSTGRES_PASSWORD": retained["postgres"], "PGPORT": "15432"})
    environment("schema", {"SQL_HOST": "127.0.0.1", "SQL_PORT": "15432", "SQL_USER": "anvilkit_temporal",
                           "SQL_PASSWORD": retained["temporal"], "SQL_PLUGIN": "postgres12"})

    def dsn(role):
        return f"postgres://anvilkit_local_{role}:{retained[role]}@127.0.0.1:15432/anvilkit_agent?sslmode=disable&connect_timeout=3"

    environment("api", {
        "ANVILKIT_API_PUBLIC_LISTEN": "127.0.0.1:18080", "ANVILKIT_API_PRIVATE_LISTEN": "127.0.0.1:18081",
        "ANVILKIT_API_CONTROL_ENDPOINT": "https://127.0.0.1:18082", "ANVILKIT_API_READ_DSN": dsn("api") + "&pool_max_conns=2",
        "ANVILKIT_API_CONTROL_VALIDATION_TOKEN": retained["validation"], "ANVILKIT_API_CONTROL_TIMEOUT_SECONDS": "10",
        "ANVILKIT_API_IDENTITY_PROFILE": "/run/local/identities.json", "ANVILKIT_API_PROFILE": "controlled-local",
        "ANVILKIT_API_SERVICE_INSTANCE_ID": "compose-api", "ANVILKIT_API_ENVIRONMENT": "compose-local",
        "ANVILKIT_API_CONTROL_CA": "/run/local/control-ca.crt", "ANVILKIT_API_CONTROL_CERT": "/run/local/client.crt",
        "ANVILKIT_API_CONTROL_KEY": "/run/local/client.key",
    })
    for name in ("control", "workflow"):
        prefix = "ANVILKIT_" + name.upper() + "_TEMPORAL_"
        values = {prefix + key: value for key, value in {
            "ENDPOINT": "127.0.0.1:17233", "NAMESPACE": "anvilkit-local-check", "TLS_SERVER_NAME": "temporal.local",
            "TLS_CA": "/run/local/temporal-ca.crt" if name == "control" else "/run/local/ca.crt",
            "TLS_CERT": "/run/local/temporal-client.crt" if name == "control" else "/run/local/client.crt",
            "TLS_KEY": "/run/local/temporal-client.key" if name == "control" else "/run/local/client.key",
        }.items()}
        if name == "control":
            values.update({
                "ANVILKIT_CONTROL_LISTEN_ADDR": "127.0.0.1:18082", "ANVILKIT_CONTROL_DATABASE_URL": dsn("control"),
                "ANVILKIT_CONTROL_DEVELOPMENT_TOKEN": retained["validation"], "ANVILKIT_CONTROL_DISCLOSURE_PROFILE": "/run/local/profile.json",
                "ANVILKIT_CONTROL_DISCLOSURE_EVIDENCE": "/run/local/evidence.json", "ANVILKIT_CONTROL_TLS_CLIENT_CA": "/run/local/api-ca.crt",
                "ANVILKIT_CONTROL_TLS_CERT": "/run/local/server.crt", "ANVILKIT_CONTROL_TLS_KEY": "/run/local/server.key",
                "ANVILKIT_CONTROL_LOCAL_CHECK_PROFILE": "local-check-v1", "ANVILKIT_CONTROL_ENVIRONMENT": "compose-local",
                "ANVILKIT_CONTROL_INTAKE_DIRECTORY": "/var/lib/anvilkit/intake",
                # Preparations (S2): the artifact store and the Workflow service credential.
                "ANVILKIT_CONTROL_ARTIFACT_DIRECTORY": "/var/lib/anvilkit/artifacts", "ANVILKIT_CONTROL_WORKFLOW_TOKEN": retained["workflow"],
            })
        else:
            values.update({
                "ANVILKIT_WORKFLOW_CONTROL_ENDPOINT": "https://127.0.0.1:18082", "ANVILKIT_WORKFLOW_CONTROL_CA": "/run/local/control-ca.crt",
                "ANVILKIT_WORKFLOW_CONTROL_CERT": "/run/local/control-client.crt", "ANVILKIT_WORKFLOW_CONTROL_KEY": "/run/local/control-client.key",
                "ANVILKIT_WORKFLOW_CONTROL_TOKEN": retained["workflow"],
            })
        environment(name, values)

    server_tls = {"certFile": "/run/local/server.crt", "keyFile": "/run/local/server.key",
                  "requireClientAuth": True, "clientCaFiles": ["/run/local/ca.crt"]}
    client_tls = {"serverName": "temporal.local", "rootCaFiles": ["/run/local/ca.crt"]}
    config = {
        "log": {"stdout": True, "level": "warn"},
        "persistence": {"defaultStore": "default", "visibilityStore": "visibility", "numHistoryShards": 4, "datastores": {}},
        "global": {"membership": {"maxJoinDuration": "30s", "broadcastAddress": "127.0.0.1"},
                   "tls": {"internode": {"server": server_tls, "client": client_tls}, "frontend": {"server": server_tls, "client": client_tls}}},
        "services": {},
        "clusterMetadata": {"enableGlobalNamespace": False, "failoverVersionIncrement": 10, "masterClusterName": "active",
                            "currentClusterName": "active", "clusterInformation": {"active": {"enabled": True,
                            "initialFailoverVersion": 1, "rpcName": "frontend", "rpcAddress": "127.0.0.1:17233"}}},
        "dcRedirectionPolicy": {"policy": "noop"},
    }
    for store, database in (("default", "temporal"), ("visibility", "temporal_visibility")):
        config["persistence"]["datastores"][store] = {"sql": {
            "pluginName": "postgres12", "databaseName": database, "connectAddr": "127.0.0.1:15432", "connectProtocol": "tcp",
            "user": "anvilkit_temporal", "password": retained["temporal"], "maxConns": 10, "maxIdleConns": 10, "maxConnLifetime": "1h",
        }}
    for offset, name in enumerate(("frontend", "history", "matching", "worker")):
        config["services"][name] = {"rpc": {"grpcPort": 17233 + offset, "membershipPort": 16933 + offset, "bindOnLocalHost": True}}
    document(STATE / "temporal/server.yaml", config, 0o444)
    print("Prepared .local/compose; passwords and caller tokens retained, certificates and evidence valid until " + expiry)
    print("API: http://127.0.0.1:18080; start with docker compose up --build -d --wait")


if __name__ == "__main__":
    main()
