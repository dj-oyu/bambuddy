"""Refresh the captured BMCU monitor schema from the bridge itself.

The bridge self-describes at ``GET /api/schema.json``: every enum and every
structure layout needed to decode its binary endpoints, generated on the BMCU
side from ``docs/bmcu_wire_layout.json``. BMCU_BINARY_TRANSPORT_V1.md section 12
makes that the consumer's entry point and says no copy of the generator's input
belongs in this tree -- bambuddy carried one for a while and it had already
drifted from the device in three places.

So the fixture under backend/tests is a capture, not a contract: whatever the
bridge served, byte for byte. This script is the only thing that should write
it. ``revision`` is the staleness signal -- the device bumps it whenever a
structure or an enum moves -- so the diff this prints is the list of decoder
assumptions that need re-reading before the refresh is committed.

Usage:
    python3 scripts/fetch_bmcu_schema.py [url]

    BMCU_MONITOR_URL=http://bmcu-monitor-b.local python3 scripts/fetch_bmcu_schema.py

Exits non-zero if the bridge is unreachable or answers with something that is
not a schema, leaving the existing capture untouched.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "backend/tests/fixtures/bmcu_binary/monitor_schema.json"
DEFAULT_URL = os.environ.get("BMCU_MONITOR_URL", "http://bmcu-monitor-a.local")
TIMEOUT_S = 10


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(f"{url.rstrip('/')}/api/schema.json", timeout=TIMEOUT_S) as response:
        return response.read()


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    try:
        raw = fetch(url)
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not reach {url}: {exc}", file=sys.stderr)
        return 1
    try:
        served = json.loads(raw)
        revision = served["revision"]
        structures = served["structures"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"{url} did not answer with a schema: {exc}", file=sys.stderr)
        return 1

    previous = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
    if previous.get("revision") == revision and previous.get("structures") == structures:
        print(f"capture already at revision {revision}; nothing moved")
        return 0

    for name in sorted(set(structures) | set(previous.get("structures", {}))):
        if structures.get(name) != previous.get("structures", {}).get(name):
            print(f"  structure changed: {name}")
    print(f"revision {previous.get('revision', '-')} -> {revision}")

    FIXTURE.write_bytes(raw)
    print(f"wrote {FIXTURE}")
    print("Re-read the changed structures, then update STATUS_SCHEMA_REVISION in bmcu_decoder.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
