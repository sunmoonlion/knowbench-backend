"""Execution limits for proposing data, independent of author methods.

Only the authenticated data-plan attempt uses these limits. Observation uses
completed app-server tool events; neither model text nor caller args select it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

DATA_STEP_ID = "data-plan"
DATA_PLAN_SECONDS = 180
MAX_IDENTICAL_RESULTS = 3
MAX_TOOL_CALLS = 40
# Native search is absent from this phase's tool configuration. A provider
# returning it anyway is a capability/protocol mismatch, not useful progress.
MAX_WEB_SEARCHES = 1

STOP_REASONS = {
    "repeated_result": "准备数据时重复调用同一工具并得到相同结果，未形成新的数据需求",
    "tool_limit": "准备数据阶段工具调用过多，尚未结束数据需求规划",
    "web_search_limit": "模型调用了本阶段未开放的内置搜索，尚未形成数据需求",
    "time_limit": "准备数据超过3分钟，尚未完成数据需求规划",
}


@dataclass
class DataPlanningGuard:
    seen: set[str] = field(default_factory=set)
    results: dict[str, int] = field(default_factory=dict)
    calls: int = 0
    searches: int = 0
    failure: str | None = None

    def observe(self, item: dict[str, Any]) -> None:
        if self.failure or item.get("type") not in ("mcpToolCall", "webSearch"):
            return
        # Replayed notifications must not charge the guard twice.
        identity = item.get("id")
        if not isinstance(identity, str) or identity in self.seen:
            return
        self.seen.add(identity)
        self.calls += 1
        if self.calls >= MAX_TOOL_CALLS:
            self.failure = "tool_limit"
            return
        if item["type"] == "webSearch":
            self.searches += 1
            if self.searches >= MAX_WEB_SEARCHES:
                self.failure = "web_search_limit"
            return
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    item.get("server"),
                    item.get("tool"),
                    item.get("arguments"),
                    item.get("result"),
                    item.get("error"),
                ],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.results[fingerprint] = self.results.get(fingerprint, 0) + 1
        if self.results[fingerprint] >= MAX_IDENTICAL_RESULTS:
            self.failure = "repeated_result"
