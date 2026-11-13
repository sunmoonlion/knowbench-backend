"""Bounded, inert Skill archives. Files never become host filesystem paths."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import posixpath
import stat
import zipfile
import zlib

from pydantic import Field

from app.domain.workbench.capabilities import CapabilityError, Strict

MAX_ARCHIVE = 2 * 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_FILE = 512 * 1024
MAX_FILES = 256


class SkillPackageCreate(Strict):
    request_id: str = Field(min_length=8, max_length=128)
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=1000)
    version: str = Field(min_length=1, max_length=50)
    archive_base64: str = Field(min_length=4, max_length=2796204, repr=False)
    dependencies: list[str] = Field(default_factory=list, max_length=30)


class SkillRead(Strict):
    skill_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    path: str = Field(min_length=1, max_length=500)
    relative_to: str = Field(default="SKILL.md", min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0, le=MAX_FILE)
    limit: int = Field(default=12000, ge=1, le=16000)


def canonical_path(path: str) -> str:
    if (
        not path
        or len(path) > 500
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(p in ("", ".", "..") for p in path.split("/"))
    ):
        raise CapabilityError("Invalid skill resource path")
    return path


def resolve_path(path: str, relative_to: str) -> str:
    canonical_path(relative_to)
    if path.startswith("/") or "\\" in path or ":" in path:
        raise CapabilityError("Invalid skill resource path")
    return canonical_path(
        posixpath.normpath(posixpath.join(posixpath.dirname(relative_to), path))
    )


def unpack_skill(encoded: str) -> dict:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CapabilityError("Skill archive must be base64 ZIP") from exc
    if len(raw) > MAX_ARCHIVE:
        raise CapabilityError("Skill ZIP exceeds 2 MiB")
    resources, files = {}, []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES * 2:
                raise CapabilityError("Too many ZIP entries")
            for entry in entries:
                if entry.orig_filename != entry.filename:
                    raise CapabilityError("Invalid Skill resource filename")
                path = canonical_path(
                    entry.filename.rstrip("/") if entry.is_dir() else entry.filename
                )
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                ):
                    raise CapabilityError(
                        "Skill archives cannot contain links or special files"
                    )
                if entry.flag_bits & 1:
                    raise CapabilityError("Encrypted Skill archives are not supported")
                if entry.is_dir():
                    continue
                if path in resources:
                    raise CapabilityError("Duplicate Skill resource path")
                total += entry.file_size
                if (
                    entry.file_size > MAX_FILE
                    or total > MAX_TOTAL
                    or len(files) >= MAX_FILES
                ):
                    raise CapabilityError(
                        "Skill package exceeds file or total size limits"
                    )
                with archive.open(entry) as source:
                    data = source.read(MAX_FILE + 1)
                if len(data) != entry.file_size or len(data) > MAX_FILE:
                    raise CapabilityError("Invalid Skill resource size")
                try:
                    text = data.decode("utf-8")
                    is_text = "\x00" not in text
                except UnicodeDecodeError:
                    is_text = False
                media = "text/plain" if is_text else "application/octet-stream"
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    media = "image/png"
                elif data.startswith(b"\xff\xd8\xff"):
                    media = "image/jpeg"
                resources[path] = base64.b64encode(data).decode("ascii")
                files.append(
                    {
                        "path": path,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "media_type": media,
                    }
                )
    except (
        zipfile.BadZipFile,
        RuntimeError,
        NotImplementedError,
        ValueError,
        OSError,
        EOFError,
        zlib.error,
    ) as exc:
        raise CapabilityError("Invalid or unsupported Skill ZIP") from exc
    if "SKILL.md" not in resources:
        raise CapabilityError("Skill ZIP must contain SKILL.md at its root")
    try:
        content = base64.b64decode(resources["SKILL.md"]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapabilityError("SKILL.md must be UTF-8") from exc
    if not content.strip() or "\x00" in content or len(content.encode()) > 65536:
        raise CapabilityError("SKILL.md must be nonempty text up to 64 KiB")
    return {
        "content": content,
        "resources": resources,
        "files": sorted(files, key=lambda f: f["path"]),
        "digest": hashlib.sha256(raw).hexdigest(),
        "format": "skill-package-zip",
    }


def skill_files(detail: dict) -> list[dict]:
    if detail.get("format") == "skill-package-zip":
        return detail["files"]
    data = detail["content"].encode("utf-8")
    return [
        {
            "path": "SKILL.md",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "media_type": "text/plain",
        }
    ]


def resource_result(detail: dict, request: SkillRead) -> dict:
    resources = detail.get("resources") or {
        "SKILL.md": base64.b64encode(detail["content"].encode("utf-8")).decode("ascii")
    }
    if request.relative_to not in resources:
        raise CapabilityError("Unknown Skill reference source")
    path = resolve_path(request.path, request.relative_to)
    if path not in resources:
        raise CapabilityError("Skill resource not found")
    meta = next(f for f in skill_files(detail) if f["path"] == path)
    result = {"skill_id": request.skill_id, "version": detail["version"], **meta}
    if meta["media_type"].startswith("image/"):
        return {**result, "image_base64": resources[path]}
    if meta["media_type"] != "text/plain":
        return {
            **result,
            "message": "Binary resource preserved; this reader supports text and PNG/JPEG images only.",
        }
    text = base64.b64decode(resources[path]).decode("utf-8")
    end = min(request.offset + request.limit, len(text))
    return {
        **result,
        "text": text[request.offset : end],
        "offset": request.offset,
        "next_offset": end if end < len(text) else None,
    }
