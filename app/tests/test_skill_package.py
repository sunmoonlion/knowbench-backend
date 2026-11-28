import base64
import io
import stat
import zipfile

import pytest

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.skill_package import (
    SkillRead,
    instruction_package,
    resource_metadata,
    resource_result,
    unpack_skill,
)

SID = "11111111-1111-4111-8111-111111111111"


def archive(files=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in (
            files
            or {
                "SKILL.md": "Read references/guide.md",
                "references/guide.md": "REFERENCE_SENTINEL",
                "scripts/example.py": "raise RuntimeError('must never run')",
            }
        ).items():
            z.writestr(name, content)
    return base64.b64encode(output.getvalue()).decode()


def read_resource(package, request):
    file = resource_metadata(package["files"], request)
    return resource_result(
        package["version"], file, package["resources"][file["path"]], request
    )


def test_roundtrip_relative_references_and_character_pagination():
    data = unpack_skill(
        archive(
            {
                "SKILL.md": "entry",
                "skills/a/SKILL.md": "../b/guide.md",
                "skills/b/guide.md": "中文证据",
            }
        )
    )
    data["version"] = "v1"
    result = read_resource(
        data,
        SkillRead(
            skill_id=SID,
            path="../b/guide.md",
            relative_to="skills/a/SKILL.md",
            offset=1,
            limit=2,
        ),
    )
    assert result["text"] == "文证" and result["next_offset"] == 3
    assert result["path"] == "skills/b/guide.md"
    assert data["content"] == "entry" and len(data["files"]) == 3


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/etc/passwd",
        "a/../../escape",
        "a\\b",
        "C:/x",
        "a//b",
        "a/./b",
        "bad\x01file",
    ],
)
def test_archive_paths_are_not_filesystem_paths(path):
    with pytest.raises(CapabilityError):
        unpack_skill(archive({"SKILL.md": "safe", path: "bad"}))


def test_symlink_duplicate_size_and_missing_entry_are_rejected():
    entry = zipfile.ZipInfo("link")
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(CapabilityError, match="links"):
        unpack_skill(archive({"SKILL.md": "safe", entry: "target"}))
    with pytest.raises(CapabilityError, match="size"):
        unpack_skill(archive({"SKILL.md": "safe", "bomb": "x" * (512 * 1024 + 1)}))
    with pytest.raises(CapabilityError, match="root"):
        unpack_skill(archive({"nested/SKILL.md": "wrong"}))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("SKILL.md", "first")
        with pytest.warns(UserWarning):
            z.writestr("SKILL.md", "second")
    with pytest.raises(CapabilityError, match="Duplicate"):
        unpack_skill(base64.b64encode(output.getvalue()).decode())


def test_reference_escape_and_unknown_source_rejected():
    data = {**unpack_skill(archive()), "version": "1"}
    for path, source in [("../secret", "SKILL.md"), ("SKILL.md", "missing.md")]:
        with pytest.raises(CapabilityError):
            read_resource(data, SkillRead(skill_id=SID, path=path, relative_to=source))


def test_plain_instruction_uses_same_lazy_reader():
    result = read_resource(
        {**instruction_package("on demand"), "version": "1"},
        SkillRead(skill_id=SID, path="SKILL.md"),
    )
    assert result["text"] == "on demand"


@pytest.mark.parametrize("encoded", ["%%%%", base64.b64encode(b"not zip").decode()])
def test_invalid_archive(encoded):
    with pytest.raises(CapabilityError):
        unpack_skill(encoded)


def test_image_bytes_round_trip_and_opaque_binary_only_returns_metadata():
    image = b"\x89PNG\r\n\x1a\n\xff"
    binary = b"\x00\xff"
    package = {
        **unpack_skill(
            archive({"SKILL.md": "entry", "img.png": image, "raw.bin": binary})
        ),
        "version": "1",
    }
    png = read_resource(package, SkillRead(skill_id=SID, path="img.png"))
    assert png["media_type"] == "image/png"
    assert base64.b64decode(png["image_base64"]) == image
    raw = read_resource(package, SkillRead(skill_id=SID, path="raw.bin"))
    assert raw["media_type"] == "application/octet-stream"
    assert raw["size"] == len(binary) and "message" in raw
    assert {"text", "image_base64"}.isdisjoint(raw)


def test_script_and_resource_paths_share_one_directory_tree():
    from app.domain.workbench.skill_execution import is_script_path

    assert is_script_path("jobs/run.py")
    assert not is_script_path("jobs/.py")
    assert unpack_skill(
        archive({"SKILL.md": "entry", "assets/data": "rows", "jobs/run.py": "pass"})
    )
    with pytest.raises(CapabilityError, match="also a directory: assets"):
        unpack_skill(
            archive({"SKILL.md": "entry", "assets/data": "rows", "assets": "table"})
        )
