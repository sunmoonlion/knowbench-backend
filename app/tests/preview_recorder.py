"""录样例：把接口的返回按情景存下来，给网页端的预览用（网页端 `preview/fixtures/`）。

样例不手写。各种状态由真的后端在测试库里造出来，这里只负责把返回存下来。
这个文件在模板与各应用里逐字相同。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
# 接口的返回里冒号后面没有空格，事件流里有：两种都认
TOKEN = re.compile(r'("token":\s*")([A-Za-z0-9_-]{40,})(")')
NAMESPACE = uuid.UUID("5c0a7e1e-0000-4000-8000-000000000000")


class Recorder:
    """把接口的返回按情景存下来。编号换成固定的，重新录一遍地址不变。"""

    def __init__(self, scenario: str, *, title: str, description: str) -> None:
        self.scenario = scenario
        self.title = title
        self.description = description
        self.responses: list[dict[str, Any]] = []
        self.streams: list[dict[str, Any]] = []
        self.pages: list[dict[str, str]] = []
        self.signed_in = True
        self.user: dict[str, Any] = {"display_name": "样例用户"}
        self.ids: dict[str, str] = {}
        self.tokens: dict[str, str] = {}

    # ---------------- 记 ----------------
    async def get(self, http, path: str, *, expect: int = 200, **params: Any) -> Any:
        return await self.call(http, "GET", path, expect=expect, params=params or None)

    async def call(
        self,
        http,
        method: str,
        path: str,
        *,
        expect: int | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        answer = await http.request(method, path, params=params, json=json_body)
        if expect is not None:
            assert answer.status_code == expect, (
                method,
                path,
                answer.status_code,
                answer.text,
            )
        kind = answer.headers.get("content-type", "application/json")
        self.responses.append(
            {
                "method": method,
                "path": answer.request.url.path,
                "query": answer.request.url.query.decode("ascii"),
                "status": answer.status_code,
                "content_type": kind,
                "text": answer.text,
            }
        )
        return answer.json() if "json" in kind and answer.text else answer.text

    def stream(self, path: str, events: list[dict[str, Any]]) -> None:
        self.streams.append({"path": path, "events": events})

    def page(self, title: str, path: str) -> None:
        self.pages.append({"title": title, "path": path})

    # ---------------- 存 ----------------
    def steady(self, text: str) -> str:
        def one(found: re.Match) -> str:
            seen = found.group(0).lower()
            if seen not in self.ids:
                made = uuid.uuid5(NAMESPACE, f"{self.scenario}:{len(self.ids)}")
                self.ids[seen] = str(made)
            return self.ids[seen]

        def secret(found: re.Match) -> str:
            seen = found.group(2)
            if seen not in self.tokens:
                self.tokens[seen] = f"preview-token-{len(self.tokens):04d}".ljust(
                    43, "0"
                )
            return found.group(1) + self.tokens[seen] + found.group(3)

        return TOKEN.sub(secret, UUID.sub(one, text))

    def write(self, root: Path) -> Path:
        directory = root / self.scenario
        directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=root, prefix=".recording-") as tmp:
            stage = Path(tmp)
            self._write_files(stage)
            manifest = stage / "manifest.json"
            # Readers open the manifest after its immutable bodies are installed.
            lock = os.open(directory, os.O_RDONLY)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                previous = directory / "manifest.json"
                old_manifest = (
                    json.loads(previous.read_bytes()) if previous.exists() else {}
                )
                keep = {item.name for item in stage.iterdir()}
                for item in stage.iterdir():
                    if item != manifest:
                        item.replace(directory / item.name)
                manifest.replace(previous)
                for key in ("responses", "streams"):
                    for entry in old_manifest.get(key, []):
                        name = entry.get("file", "")
                        if (
                            re.fullmatch(
                                r"(stream-)?[0-9]{4,}(-[0-9a-f]{64})?\.(json|md)", name
                            )
                            and name not in keep
                        ):
                            (directory / name).unlink(missing_ok=True)
            finally:
                os.close(lock)
        return directory

    def _write_files(self, directory: Path) -> None:
        # 先按记下来的先后把编号都认一遍，地址里的编号才和内容里的一致
        kept: dict[tuple[str, str, str], dict[str, Any]] = {}
        for response in self.responses:
            body = self.steady(response["text"])
            path = self.steady(response["path"])
            query = self.steady(response["query"])
            # 同一个地址记了几次的，留最后一次：那是最终的样子
            kept[(response["method"], path, query)] = {**response, "text": body}
        manifest_responses = []
        for n, ((method, path, query), response) in enumerate(kept.items(), start=1):
            is_json = "json" in response["content_type"]
            body = response["text"]
            if is_json and body:
                body = json.dumps(json.loads(body), ensure_ascii=False, indent=1)
            payload = (body + "\n" if body else "").encode()
            digest = hashlib.sha256(payload).hexdigest()
            name = f"{n:04d}-{digest}.{'json' if is_json else 'md'}"
            (directory / name).write_bytes(payload)
            manifest_responses.append(
                {
                    "method": method,
                    "path": path,
                    "query": query,
                    "status": response["status"],
                    "content_type": response["content_type"],
                    "file": name,
                }
            )
        manifest_streams = []
        for n, stream in enumerate(self.streams, start=1):
            body = self.steady(
                json.dumps(stream["events"], ensure_ascii=False, default=str)
            )
            payload = (
                json.dumps(json.loads(body), ensure_ascii=False, indent=1) + "\n"
            ).encode()
            name = f"stream-{n:04d}-{hashlib.sha256(payload).hexdigest()}.json"
            (directory / name).write_bytes(payload)
            manifest_streams.append({"path": self.steady(stream["path"]), "file": name})
        manifest = {
            "id": self.scenario,
            "title": self.title,
            "description": self.description,
            "signed_in": self.signed_in,
            "user": self.user,
            "pages": [
                {"title": p["title"], "path": self.steady(p["path"])}
                for p in self.pages
            ],
            "responses": manifest_responses,
            "streams": manifest_streams,
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1) + "\n"
        )
