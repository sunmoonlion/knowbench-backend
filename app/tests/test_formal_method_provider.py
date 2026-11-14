"""Consumer source and schema must remain an exact provider-generated projection."""

import hashlib
import json
from pathlib import Path

from app.domain.workbench.formal_method_generated import FormalMethodDefinition


def test_formal_method_schema_and_source_match_provider():
    app = Path(__file__).resolve().parents[1]
    lock = json.loads((app / "contracts/formal-method-lock.json").read_text())
    schema = (app / "contracts/formal-method-v1.json").read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == lock["schema_sha256"]
    own = FormalMethodDefinition.model_json_schema()
    supplied = json.loads(schema)
    for key in ("$defs", "properties", "required", "additionalProperties"):
        assert supplied[key] == own[key]
    provider = app.parents[2] / "knowlab-app/knowlab-backend/app/app/domain/knowbots"
    if not provider.exists():
        return  # The application image has the pinned schema and generated code only.
    for name, expected in lock["source_sha256"].items():
        assert hashlib.sha256((provider / name).read_bytes()).hexdigest() == expected
