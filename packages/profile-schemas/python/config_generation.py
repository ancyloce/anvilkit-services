#!/usr/bin/env python3
"""Executable Python consumer of the shared configuration schemas (DD-09 §4/§5)
for the Inference service (P15 integrates it): immutable configuration
generations built from defaults < a reviewed YAML file < a validated,
unexpired Apollo snapshot (non-secret keys) < the allowlisted environment,
validated as a whole with jsonschema and pydantic, with the secret taken from
the environment or a mounted file and never placed in a digest or a log.

    python3 config_generation.py --fixtures ../fixtures.json      # the shared cases
    python3 config_generation.py --config config.yaml --snapshot snapshot.json --env-prefix ANVILKIT_INFERENCE_

Standard library plus jsonschema, pydantic and PyYAML (the pinned toolchain of
tools/requirements.txt); no configuration platform client.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent

SECRET_OR_PLACEMENT = re.compile(r"(^|\.)(url|url_file|address|token|secret|password|access_key_id|secret_access_key|snapshot_file)$")


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def validate_snapshot(instance: Any, app_id: str, now: datetime) -> dict[str, Any]:
    """The snapshot rules: the schema, this service's appId, the instant."""
    jsonschema.Draft202012Validator(_schema("apollo-snapshot")).validate(instance)
    if instance["appId"] != app_id:
        raise ValueError(f"apollo snapshot: appId {instance['appId']} is not this service ({app_id})")
    fetched = datetime.fromisoformat(instance["fetchedAt"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(instance["expiresAt"].replace("Z", "+00:00"))
    if expires <= fetched:
        raise ValueError("apollo snapshot: expiresAt must follow fetchedAt")
    if expires <= now:
        raise ValueError(f"apollo snapshot: expired at {instance['expiresAt']}; an expired snapshot never starts a generation")
    return instance


def validate_generation_record(instance: Any) -> dict[str, Any]:
    jsonschema.Draft202012Validator(_schema("config-generation")).validate(instance)
    return instance


class Inference(BaseModel):
    """The reviewed bounds of the Inference service (an example shape for P15)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    listen: str = Field(default="127.0.0.1:9108", pattern=r"^[^:\s]+:\d{1,5}$")
    embedding_model: str = Field(default="bge-m3", min_length=1, max_length=128)
    max_batch: int = Field(default=32, ge=1, le=1024)
    max_texts_per_request: int = Field(default=64, ge=1, le=4096)
    request_timeout_ms: int = Field(default=30_000, ge=100, le=600_000)

    @model_validator(mode="after")
    def cross_fields(self) -> "Inference":
        if self.max_texts_per_request > self.max_batch * 16:
            raise ValueError("max_texts_per_request must not exceed 16 batches")
        return self


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int
    config: Inference
    digest: str
    secret_revision: str
    apollo_release: str = ""
    profiles: dict[str, str]

    def record(self, service: str = "anvilkit-agent-inference") -> dict[str, Any]:
        r = {
            "schemaVersion": 1,
            "service": service,
            "configGeneration": str(self.number),
            "digest": self.digest,
            "secretRevision": self.secret_revision,
            "profiles": self.profiles,
        }
        if self.apollo_release:
            r["apolloRelease"] = self.apollo_release
        return validate_generation_record(r)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def load_generation(config_file: Path | None, snapshot_file: Path | None, environ: dict[str, str], env_prefix: str, app_id: str, number: int = 1, now: datetime | None = None) -> Generation:
    """defaults < file < validated snapshot < allowlisted environment; the secret from the environment or a file."""
    now = now or datetime.now(timezone.utc)
    raw: dict[str, Any] = {}
    if config_file:
        loaded = yaml.safe_load(config_file.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file {config_file}: not a mapping")
        for key in loaded:
            if SECRET_OR_PLACEMENT.search(str(key)):
                raise ValueError(f"config file {config_file}: {key} is a secret or a placement and is accepted only from the environment")
        raw.update(loaded)
    apollo_release = ""
    if snapshot_file:
        snap = validate_snapshot(json.loads(snapshot_file.read_text()), app_id, now)
        for key, value in snap["configurations"].items():
            raw[key] = value
        apollo_release = snap["releaseKey"]
    allowed = {f"{env_prefix}LISTEN": "listen", f"{env_prefix}MODEL_TOKEN": "__secret__", f"{env_prefix}MODEL_TOKEN_FILE": "__secret_file__"}
    secret = ""
    unknown = []
    for name, value in environ.items():
        if not name.startswith(env_prefix) or name == f"{env_prefix}CONFIG":
            continue
        key = allowed.get(name)
        if key is None:
            unknown.append(name)
        elif key == "__secret__":
            secret = value
        elif key == "__secret_file__":
            secret = Path(value).read_text().strip()
        else:
            raw[key] = value
    if unknown:
        raise ValueError("environment variables are not allowed overrides: " + ", ".join(sorted(unknown)))
    # Numbers from a snapshot or the environment arrive as strings.
    for key in ("max_batch", "max_texts_per_request", "request_timeout_ms"):
        if key in raw and isinstance(raw[key], str) and raw[key].isdigit():
            raw[key] = int(raw[key])
    try:
        cfg = Inference(**raw)
    except ValidationError as err:
        raise ValueError(f"config: {err}") from None
    return Generation(
        number=number,
        config=cfg,
        digest=_digest(json.dumps(raw, sort_keys=True, separators=(",", ":"))),
        secret_revision=_digest("model.token=" + secret),
        apollo_release=apollo_release,
        profiles={"embedding-bge-m3-v1": _digest(cfg.embedding_model)},
    )


def run_fixtures(path: Path) -> int:
    doc = json.loads(path.read_text())
    now = datetime.fromisoformat(doc["now"].replace("Z", "+00:00"))
    failures = 0
    for case in doc["cases"]:
        try:
            if case["schema"] == "apollo-snapshot":
                validate_snapshot(case["instance"], case["appId"], now)
            else:
                validate_generation_record(case["instance"])
            ok = True
        except (jsonschema.ValidationError, ValueError):
            ok = False
        status = "PASS" if ok == case["valid"] else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"{status} {case['name']}")
    print(f"profile-schemas python: {len(doc['cases'])} cases, {failures} failures")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fixtures", type=Path, help="run the shared fixture cases and exit")
    p.add_argument("--config", type=Path, help="the reviewed YAML file")
    p.add_argument("--snapshot", type=Path, help="a validated Apollo snapshot")
    p.add_argument("--env-prefix", default="ANVILKIT_INFERENCE_")
    p.add_argument("--app-id", default="anvilkit-agent-inference")
    a = p.parse_args(argv)
    if a.fixtures:
        return run_fixtures(a.fixtures)
    try:
        gen = load_generation(a.config, a.snapshot, dict(os.environ), a.env_prefix, a.app_id)
    except ValueError as err:
        print(f"rejected: {err}", file=sys.stderr)
        return 1
    print(json.dumps(gen.record(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
