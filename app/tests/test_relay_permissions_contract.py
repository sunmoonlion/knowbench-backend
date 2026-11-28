"""Consumer locks the relay-owned schema; never silently invent a wire shape."""

import hashlib
import json
from pathlib import Path


def test_provider_contract_lock():
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / "contracts/relay-permissions.lock.json").read_text())
    canonical = json.dumps(lock["contract"], sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == lock["canonical_sha256"]
    platform = root.parents[2] / "sunmoonun"
    if platform.is_dir():
        provider = (
            platform
            / "infrastructure/applications/component-images/relay/local-permission-v1.json"
        )
        assert json.loads(provider.read_text()) == lock["contract"]
