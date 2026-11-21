"""Validate a turn-scoped Codex permission profile against the selected Link."""

from pathlib import PurePosixPath, PureWindowsPath

from app.domain.workbench.errors import ProjectPathInvalid
from app.domain.workbench.projects import clean_relative_path, is_windows_root


def inside(path, root):
    if not isinstance(path, str) or not isinstance(root, str) or not root:
        return False
    if not path or len(path) > 2048 or not path.isprintable():
        return False
    windows = is_windows_root(root)
    cls = PureWindowsPath if windows else PurePosixPath
    target, base = cls(path), cls(root)
    if not target.is_absolute() or not base.is_absolute() or ".." in target.parts:
        return False
    try:
        relative = target.relative_to(base)
        clean_relative_path(relative.as_posix())
    except (ValueError, ProjectPathInvalid):
        return False
    return True


def permission_profile(request, project, environment):
    """Return a displayable exact grant, or None if any requested right is outside scope."""
    if not isinstance(request, dict) or set(request) - {"network", "fileSystem"}:
        return None
    ceiling = environment.get("ceiling") or {}
    if environment.get("status") != "online" or not any(
        inside(project, root) for root in environment.get("roots") or []
    ):
        return None
    grant = {}
    network = request.get("network")
    if network is not None:
        if (
            not isinstance(network, dict)
            or set(network) != {"enabled"}
            or (network["enabled"] is not None and type(network["enabled"]) is not bool)
        ):
            return None
        if network["enabled"]:
            if ceiling.get("network") is not True:
                return None
            grant["network"] = {"enabled": True}
    fs = request.get("fileSystem")
    if fs is None:
        return grant or None
    if not isinstance(fs, dict) or set(fs) - {"read", "write", "entries"}:
        return None
    checked = []
    for access in ("read", "write"):
        paths = fs.get(access)
        if paths is not None:
            if not isinstance(paths, list) or len(paths) > 64:
                return None
            checked.extend((access, path) for path in paths)
    entries = fs.get("entries", [])
    if not isinstance(entries, list) or len(entries) > 64:
        return None
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"access", "path"}:
            return None
        path = item["path"]
        if (
            not isinstance(path, dict)
            or set(path) != {"type", "path"}
            or path["type"] != "path"
        ):
            return None
        checked.append((item["access"], path["path"]))
    for access, path in checked:
        if access not in ("read", "write") or not inside(path, project):
            return None
        if access == "write" and ceiling.get("sandbox") not in (
            "workspace-write",
            "danger-full-access",
        ):
            return None
    if checked:
        grant["fileSystem"] = {
            "read": fs.get("read"),
            "write": fs.get("write"),
            **({"entries": entries} if entries else {}),
        }
    return grant or None
