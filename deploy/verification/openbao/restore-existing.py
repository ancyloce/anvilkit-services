#!/usr/bin/env python3
"""Unseal the existing local installation over verified TLS; never initialize."""
import json
import os

from bootstrap import STATE, request

os.umask(0o077)
status = request('sys/seal-status')
if not status['initialized']:
    raise SystemExit('Existing installation is not initialized; restoration cannot initialize or replace it')
if status['sealed']:
    material = json.loads((STATE / 'init.json').read_text())
    request('sys/unseal', {'key': material['keys_base64'][0]})
    del material
status = request('sys/seal-status')
assert status['initialized'] and not status['sealed']
print('PASS: existing OpenBao initialized and unsealed; no initialization or credential changes.')
