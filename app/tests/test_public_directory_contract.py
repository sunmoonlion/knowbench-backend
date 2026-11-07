import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from schema_parity import model_vs_provider
from test_expert_match import directory

from app.domain.workbench.expert_match import DirectorySnapshot, PublicExpert
from app.domain.workbench.method_catalog import PublicCapability


def test_provider_lock_and_consumer_agree_on_the_public_directory():
    parent = Path(__file__).resolve().parents[3]
    lock = json.loads(
        (parent / "contracts/knowmart-expert-directory-v2-lock.json").read_text()
    )
    schema = parent.parent / lock["provider"] / lock["schema"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    document = directory()
    jsonschema.validate(document, json.loads(schema.read_text()))
    assert (
        DirectorySnapshot.model_validate(document).model_dump(mode="json") == document
    )
    document["experts"][0]["capability_bindings"] = {"private": True}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, json.loads(schema.read_text()))
    with pytest.raises(ValidationError):
        DirectorySnapshot.model_validate(document)


def test_author_method_v2_lock_covers_actual_compiled_optional_inputs():
    parent = Path(__file__).resolve().parents[3]
    lock = json.loads(
        (parent / "contracts/knowlab-author-method-v2-lock.json").read_text()
    )
    schema = parent.parent / lock["provider"] / lock["schema"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    manifest = json.loads(
        (
            Path(__file__).parent / "fixtures/capability-referenced-manifest.json"
        ).read_text()
    )
    sources = manifest["pack"]["method_sources"].values()
    checked = 0
    for source in sources:
        definition = source["definition"]
        if definition["schema"] == "knowlab/method-definition/v2":
            jsonschema.validate(definition, json.loads(schema.read_text()))
            checked += 1
    assert checked > 0


def _provider(lock_name: str) -> dict:
    parent = Path(__file__).resolve().parents[3]
    lock = json.loads((parent / "contracts" / lock_name).read_text())
    schema = parent.parent / lock["provider"] / lock["schema"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    return json.loads(schema.read_text())


@pytest.mark.parametrize(
    "model,name",
    [(PublicCapability, "PublicCapability"), (PublicExpert, "PublicExpert")],
    ids=["PublicCapability", "PublicExpert"],
)
def test_directory_models_match_the_provider_field_by_field(model, name):
    """SDD 0052 第一节：长度、必填、正则、枚举、条数逐项和提供方一样，不靠样例。"""
    assert (
        model_vs_provider(
            model, _provider("knowmart-expert-directory-v2-lock.json"), name
        )
        == []
    )
