"""Installation resolves immutable public sources and never executes archive files."""

import io
import stat
import zipfile
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.skill_installation import (
    SkillSourceRequest,
    metadata,
    repository_packages,
)
from app.infrastructure.workbench.skill_sources import GitHubSkills

COMMIT = "a" * 40
CONTENT = "---\nname: notice\ndescription: >\n  Prepare a notice\n  and validate recipients.\n---\nRead references/guide.md before drafting.\n"


def archive(files=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as target:
        for name, value in (
            files
            or {
                "repo/skills/notice/SKILL.md": CONTENT,
                "repo/skills/notice/references/guide.md": "FULL_REFERENCE",
                "repo/skills/notice/scripts/helper.py": "raise RuntimeError('MUST_NOT_RUN')",
            }
        ).items():
            target.writestr(name, value)
    return data.getvalue()


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://github.com.evil.test/a/b",
        "https://user:secret@github.com/a/b",
        "https://github.com/a/b?token=x",
        "https://github.com/a/b/tree/main/%2e%2e",
        "https://127.0.0.1/a/b",
    ],
)
def test_install_source_rejects_credentials_and_untrusted_urls(url):
    with pytest.raises(ValidationError):
        SkillSourceRequest(url=url)


def test_complete_package_is_reproducible_and_keeps_scripts_inert():
    packages = repository_packages(archive(), "skills")
    candidate, encoded = packages[0]
    assert candidate["name"] == "notice" and candidate["files"] == 3
    assert (
        candidate["has_scripts"] and "validate recipients" in candidate["description"]
    )
    assert repository_packages(archive(), "skills", only="skills/notice") == packages
    assert repository_packages(archive(), "different") == []
    from app.domain.workbench.skill_package import unpack_skill

    package = unpack_skill(encoded)
    assert set(package["resources"]) == {
        "SKILL.md",
        "references/guide.md",
        "scripts/helper.py",
    }


@pytest.mark.parametrize(
    "path",
    [
        "repo/skills/notice/../escape",
        "repo/skills/notice/C:/escape",
        "/absolute",
        "repo/skills/notice/a\\b",
    ],
)
def test_repository_traversal_is_rejected(path):
    with pytest.raises(CapabilityError):
        repository_packages(
            archive({"repo/skills/notice/SKILL.md": CONTENT, path: "bad"})
        )


def test_symlinks_and_yaml_aliases_are_rejected():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as target:
        target.writestr("repo/SKILL.md", CONTENT)
        link = zipfile.ZipInfo("repo/secret")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(link, "/etc/passwd")
    with pytest.raises(CapabilityError):
        repository_packages(data.getvalue())
    with pytest.raises(CapabilityError):
        metadata("---\nname: &x notice\ndescription: *x\n---\nbody")


async def test_download_pins_preview_commit_and_uses_no_client_supplied_download_url():
    remote = GitHubSkills()
    remote.download = AsyncMock(
        side_effect=[('{"sha":"' + COMMIT + '"}').encode(), archive()]
    )
    result = await remote.fetch("https://github.com/owner/repo/tree/main/skills")
    assert result["commit"] == COMMIT and result["prefix"] == "skills"
    assert [call.args[0] for call in remote.download.await_args_list] == [
        "https://api.github.com/repos/owner/repo/commits/main",
        f"https://codeload.github.com/owner/repo/zip/{COMMIT}",
    ]
    remote.download = AsyncMock(return_value=archive())
    await remote.fetch("https://github.com/owner/repo", COMMIT)
    remote.download.assert_awaited_once_with(
        f"https://codeload.github.com/owner/repo/zip/{COMMIT}", limit=20 * 1024 * 1024
    )
