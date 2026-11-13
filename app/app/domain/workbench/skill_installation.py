"""Public repository sources and inert, reproducible Skill installation packages."""

from __future__ import annotations

import base64
import io
import re
import stat
import zipfile
import zlib
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import Field, model_validator

from app.domain.workbench.capabilities import CapabilityError, Strict
from app.domain.workbench.method_capabilities import McpRequirement
from app.domain.workbench.skill_package import (
    MAX_FILE,
    MAX_FILES,
    MAX_TOTAL,
    canonical_path,
    unpack_skill,
)


def repository_parts(url: str) -> tuple[str, str, str | None, str]:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "github.com"
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Use a public github.com repository or directory URL")
    segments = [unquote(p) for p in parts.path.strip("/").split("/")]
    if len(segments) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in (".", "..") for p in segments
    ):
        raise ValueError("Invalid GitHub repository URL")
    owner, repo = segments[:2]
    repo = repo.removesuffix(".git")
    if not repo:
        raise ValueError("Repository name is required")
    if len(segments) == 2:
        return owner, repo, None, ""
    if len(segments) < 4 or segments[2] not in ("tree", "blob"):
        raise ValueError("Use a repository, directory, or SKILL.md URL")
    path = "/".join(segments[4:])
    if segments[2] == "blob":
        if not path or path.split("/")[-1] != "SKILL.md":
            raise ValueError("File links must point to SKILL.md")
        path = path.removesuffix("SKILL.md").rstrip("/")
    return owner, repo, segments[3], path


class SkillSourceRequest(Strict):
    url: str = Field(min_length=15, max_length=1000)

    @model_validator(mode="after")
    def public_repository(self):
        repository_parts(self.url)
        return self


class SkillSource(Strict):
    repository: str
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    path: str


class SkillCandidate(Strict):
    path: str
    name: str
    description: str
    digest: str
    files: int
    has_scripts: bool
    instructions: str


class SkillSourcePreview(Strict):
    repository: str
    commit: str
    candidates: list[SkillCandidate]


class SkillInstall(SkillSourceRequest):
    request_id: str = Field(min_length=8, max_length=128)
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    path: str = Field(max_length=500)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    dependencies: list[str] = Field(default_factory=list, max_length=30)
    tool_requirements: list[McpRequirement] = Field(
        default_factory=list, max_length=30, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def selected_directory(self):
        if self.path:
            canonical_path(self.path)
        return self


def metadata(content: str) -> tuple[str, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise CapabilityError(
            "Repository Skills require name and description in SKILL.md frontmatter"
        )
    end = next(
        (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None
    )
    if end is None:
        raise CapabilityError("Invalid SKILL.md frontmatter")
    header = "\n".join(lines[1:end])
    try:
        depth = 0
        for index, event in enumerate(yaml.parse(header)):
            if isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                depth -= 1
            if index > 2000 or depth > 20 or isinstance(event, yaml.AliasEvent):
                raise CapabilityError("Skill metadata exceeds structural limits")
        values = yaml.safe_load(header)
    except (yaml.YAMLError, RecursionError) as exc:
        raise CapabilityError("Invalid Skill metadata") from exc
    name, description = (
        (values.get("name"), values.get("description"))
        if isinstance(values, dict)
        else (None, None)
    )
    if not isinstance(name, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name
    ):
        raise CapabilityError(
            "Skill name must use letters, digits, hyphens or underscores"
        )
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1000:
        raise CapabilityError("Skill description must contain 1–1000 characters")
    return name, description.strip()


def repository_packages(
    raw: bytes, prefix: str = "", *, only: str | None = None
) -> list[tuple[dict, str]]:
    """Never extract the repository, execute scripts, or follow archive links."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000:
                raise CapabilityError(
                    "Repository contains too many files; upload a Skill ZIP instead"
                )
            names = {}
            roots = set()
            for entry in entries:
                if entry.orig_filename != entry.filename:
                    raise CapabilityError("Invalid repository filename")
                full = canonical_path(entry.filename.rstrip("/"))
                root, _, path = full.partition("/")
                roots.add(root)
                if entry.is_dir():
                    continue
                if path in names:
                    raise CapabilityError("Duplicate repository path")
                names[path] = entry
            if len(roots) != 1:
                raise CapabilityError("Repository archive must have one root")
            directories = sorted(
                p.removesuffix("SKILL.md").rstrip("/")
                for p in names
                if p == "SKILL.md" or p.endswith("/SKILL.md")
            )
            directories = [
                d
                for d in directories
                if not prefix or d == prefix or d.startswith(prefix + "/")
            ]
            if only is not None:
                directories = [d for d in directories if d == only]
            if len(directories) > 50:
                raise CapabilityError(
                    "More than 50 Skills found; use a specific directory URL"
                )
            result = []
            preview_bytes = 0
            for directory in directories:
                output, count, total = io.BytesIO(), 0, 0
                with zipfile.ZipFile(
                    output, "w", compression=zipfile.ZIP_DEFLATED
                ) as target:
                    for path, entry in sorted(names.items()):
                        if directory and not path.startswith(directory + "/"):
                            continue
                        local = path[len(directory) + 1 :] if directory else path
                        mode = entry.external_attr >> 16
                        if (
                            stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                            or entry.flag_bits & 1
                        ):
                            raise CapabilityError(
                                "Skill packages cannot contain links, special or encrypted files"
                            )
                        count += 1
                        total += entry.file_size
                        if (
                            count > MAX_FILES
                            or total > MAX_TOTAL
                            or entry.file_size > MAX_FILE
                        ):
                            raise CapabilityError("Skill exceeds package size limits")
                        with archive.open(entry) as stream:
                            data = stream.read(MAX_FILE + 1)
                        if len(data) != entry.file_size:
                            raise CapabilityError("Invalid Skill file size")
                        info = zipfile.ZipInfo(local, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        target.writestr(info, data)
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
                preview_bytes += total
                if preview_bytes > 8 * 1024 * 1024:
                    raise CapabilityError(
                        "Skill previews exceed 8 MiB; use a specific directory URL"
                    )
                package = unpack_skill(encoded)
                name, description = metadata(package["content"])
                result.append(
                    (
                        {
                            "path": directory,
                            "name": name,
                            "description": description,
                            "digest": package["digest"],
                            "files": count,
                            "has_scripts": any(
                                f["path"].startswith("scripts/")
                                for f in package["files"]
                            ),
                            "instructions": package["content"],
                        },
                        encoded,
                    )
                )
            return result
    except (
        zipfile.BadZipFile,
        OSError,
        ValueError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ) as exc:
        raise CapabilityError("Invalid repository archive") from exc
