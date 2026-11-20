"""用了多少、花了多少（估算）。

模型每调用一次，Codex 报一次用量：这一次的（`last`）和这条线从头到现在的累计（`total`）。
算钱只用「这一次的」逐次相加。2026-10-04 之前的代码把累计当成了一步的用量，越往后的步骤
算得越多：第一期联调里两步的烟测记了 1.02，就是这个错。

这里算出来的是估算：用的是用户自己的模型 key，真正的账单在模型厂商那边。
单价来自配置里的价目表；不认识的模型不猜单价，只给用量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

MILLION = Decimal(1_000_000)
CENT = Decimal("0.000001")  # 账里存六位小数

# Kimi 开放平台的公开价目表，2026-10-04 查的。每百万 token，人民币元。
# 缓存写入按 5 分钟那一档算（Codex 不告诉我们用的是哪一档；1 小时档是 40 元）。
DEFAULT_PRICES_JSON = json.dumps(
    {
        "as_of": "2026-10-04",
        "currency": "CNY",
        "models": {
            "kimi-k3": {
                "input": "20",
                "cached_input": "2",
                "cache_write": "20",
                "output": "100",
                "source": "https://platform.kimi.com/docs/pricing/chat-k3",
            }
        },
    },
    ensure_ascii=False,
)


@dataclass(frozen=True)
class Price:
    """一个模型的单价，每百万 token。"""

    input: Decimal  # 没有命中缓存的输入
    cached_input: Decimal  # 命中缓存的输入
    cache_write: Decimal  # 写进缓存的输入
    output: Decimal  # 输出，含思考
    source: str = ""


@dataclass(frozen=True)
class PriceList:
    as_of: str
    currency: str
    models: dict[str, Price]

    def of(self, model: str | None) -> Price | None:
        return self.models.get(model or "")


@dataclass(frozen=True)
class Usage:
    """一次调用（或若干次相加）的用量。四项互不重叠。"""

    input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.input + self.cached_input + self.cache_write + self.output

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input + other.input,
            self.cached_input + other.cached_input,
            self.cache_write + other.cache_write,
            self.output + other.output,
        )

    def since(self, earlier: Usage) -> Usage | None:
        """比 earlier 多出来的。有哪一项变小了就返回 None：这不是同一个累计。"""
        more = Usage(
            self.input - earlier.input,
            self.cached_input - earlier.cached_input,
            self.cache_write - earlier.cache_write,
            self.output - earlier.output,
        )
        if min(more.input, more.cached_input, more.cache_write, more.output) < 0:
            return None
        return more

    @classmethod
    def from_json(cls, noted: Any) -> Usage | None:
        if not isinstance(noted, dict):
            return None
        return cls(
            _count(noted.get("input")),
            _count(noted.get("cached_input")),
            _count(noted.get("cache_write")),
            _count(noted.get("output")),
        )

    @classmethod
    def from_responses(cls, usage: dict) -> Usage:
        """Complete Responses usage for a hard-budget request; reject unknown billing."""

        def count(value):
            if type(value) is not int or value < 0:
                raise ValueError("Invalid provider token count")
            return value

        if not isinstance(usage, dict) or set(usage) - {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "input_tokens_details",
            "output_tokens_details",
        }:
            raise ValueError("Unsupported provider metering fields")
        input_tokens = count(usage.get("input_tokens"))
        output_tokens = count(usage.get("output_tokens"))
        if count(usage.get("total_tokens")) != input_tokens + output_tokens:
            raise ValueError("Inconsistent provider token total")
        inputs, outputs = (
            usage.get("input_tokens_details", {}),
            usage.get("output_tokens_details", {}),
        )
        if (
            not isinstance(inputs, dict)
            or not isinstance(outputs, dict)
            or set(inputs) - {"cached_tokens", "cache_write_tokens"}
            or set(outputs) - {"reasoning_tokens"}
        ):
            raise ValueError("Unsupported provider token detail")
        cached = count(inputs.get("cached_tokens", 0))
        written = count(inputs.get("cache_write_tokens", 0))
        if (
            cached + written > input_tokens
            or count(outputs.get("reasoning_tokens", 0)) > output_tokens
        ):
            raise ValueError("Provider detail exceeds token total")
        return cls(input_tokens - cached - written, cached, written, output_tokens)

    def as_json(self) -> dict[str, int]:
        return {
            "input": self.input,
            "cached_input": self.cached_input,
            "cache_write": self.cache_write,
            "output": self.output,
            "total": self.total,
        }


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_of(reported: Any) -> Usage:
    """Codex 报的一份用量。它的「输入」里含命中缓存的和写进缓存的，这里拆开。"""
    if not isinstance(reported, dict):
        return Usage()
    cached = _count(reported.get("cachedInputTokens"))
    written = _count(reported.get("cacheWriteInputTokens"))
    everything = _count(reported.get("inputTokens"))
    output = _count(reported.get("outputTokens"))
    if not everything and not output:
        # 只报了一个总数：分不出输入输出，按输入算（单价低的一边，不往多里估）
        everything = _count(reported.get("totalTokens"))
    return Usage(
        input=max(0, everything - cached - written),
        cached_input=min(cached, everything),
        cache_write=min(written, max(0, everything - cached)),
        output=output,
    )


def call_of(token_usage: Any) -> Usage:
    """一条用量通知里「这一次调用」的用量。没有报「这一次」的，当作没有。"""
    if not isinstance(token_usage, dict):
        return Usage()
    return usage_of(token_usage.get("last"))


def cumulative_of(token_usage: Any) -> Usage | None:
    """一条用量通知里「这条线到现在的累计」。没报的返回 None。"""
    if not isinstance(token_usage, dict):
        return None
    total = token_usage.get("total")
    return usage_of(total) if isinstance(total, dict) else None


def fresh_usage(token_usage: Any, previous: Usage | None) -> Usage:
    """这一条通知带来的新用量。

    Codex 重新装载一条线时，会把上一次的用量原样再报一遍；断线的那一会儿也可能漏掉几条。
    所以按「累计比上一次多出来的」算：重复报的是零，漏掉的会补上。
    不知道上一次的累计（这条线第一次见）、没报累计、或累计变小了的时候，
    用「这一次调用」：不往多里估。
    """
    total = cumulative_of(token_usage)
    if total is not None and previous is not None:
        more = total.since(previous)
        if more is not None:
            return more
    return call_of(token_usage)


def cost_of(usage: Usage, price: Price | None) -> Decimal | None:
    """不认识的模型返回 None：不知道单价，就不给金额。"""
    if price is None:
        return None
    amount = (
        Decimal(usage.input) * price.input
        + Decimal(usage.cached_input) * price.cached_input
        + Decimal(usage.cache_write) * price.cache_write
        + Decimal(usage.output) * price.output
    ) / MILLION
    return amount.quantize(CENT)


def _price(entry: Any, *, model: str) -> Price:
    if not isinstance(entry, dict):
        raise ValueError(f"model prices: entry of {model} must be an object")
    values = {}
    for name in ("input", "cached_input", "cache_write", "output"):
        try:
            values[name] = Decimal(str(entry[name]))
        except (KeyError, ArithmeticError) as exc:
            raise ValueError(
                f"model prices: {model} needs a number for {name}"
            ) from exc
        if not values[name].is_finite() or values[name] < 0:
            raise ValueError(f"model prices: {name} of {model} must not be negative")
    return Price(source=str(entry.get("source") or ""), **values)


def parse_prices(text: str) -> PriceList:
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("model prices: not JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("models", {}), dict):
        raise ValueError("model prices: must be an object with `models`")
    return PriceList(
        as_of=str(parsed.get("as_of") or ""),
        currency=str(parsed.get("currency") or "CNY"),
        models={
            str(model): _price(entry, model=str(model))
            for model, entry in (parsed.get("models") or {}).items()
        },
    )


DEFAULT_PRICES = parse_prices(DEFAULT_PRICES_JSON)


def price_view(prices: PriceList, model: str | None) -> dict[str, Any]:
    """给页面看的：这段对话按什么单价算的。"""
    price = prices.of(model)
    return {
        "model": model,
        "priced": price is not None,
        "currency": prices.currency,
        "as_of": prices.as_of,
        "per_million": None
        if price is None
        else {
            "input": str(price.input),
            "cached_input": str(price.cached_input),
            "cache_write": str(price.cache_write),
            "output": str(price.output),
        },
        "source": price.source if price else "",
        # 用的是用户自己的模型 key：真正的账单在模型厂商那边
        "estimated": True,
    }


def _recorded(amount: Any) -> Decimal | None:
    try:
        value = Decimal(str(amount))
    except ArithmeticError:
        return None
    return value if value.is_finite() and value >= 0 else None


def conversation_usage(
    events: list[dict[str, Any]], prices: PriceList
) -> dict[str, Any]:
    """一段对话到现在用了多少、花了多少：按轮次列出，再合计。

    events 是这段对话的用量事件，按先后。金额由「每一次调用」相加，
    不拿 Codex 报的累计来算（换过线之后累计会从头再来）。
    每一次调用的金额用当时记下的那个：和页面上当时跳出来的一致，
    以后改了单价，过去的不重算。
    """
    turns: dict[str, dict[str, Any]] = {}
    everything = Usage()
    total: Decimal | None = Decimal("0")
    model = None
    for event in events:
        payload = event.get("payload") or {}
        noted = payload.get("cost") or {}
        model = noted.get("model") or model
        # 用量也用当时记下的：重复报的那几条当时就没有算
        usage = Usage.from_json(noted.get("call_tokens")) or call_of(
            payload.get("tokenUsage") or payload.get("usage")
        )
        spent = _recorded(noted.get("call")) if noted.get("priced") else None
        turn_id = str(payload.get("turnId") or "")
        turn = turns.setdefault(
            turn_id,
            {
                "turn_id": turn_id or None,
                "calls": 0,
                "usage": Usage(),
                "cost": Decimal("0"),
            },
        )
        turn["calls"] += 1
        turn["usage"] = turn["usage"] + usage
        turn["at"] = event.get("created_at")
        everything = everything + usage
        if spent is None:
            turn["cost"] = None
            total = None
        else:
            if turn["cost"] is not None:
                turn["cost"] += spent
            if total is not None:
                total += spent
    return {
        "price": price_view(prices, model),
        "cost": None if total is None else str(total),
        "tokens": everything.as_json(),
        "calls": sum(t["calls"] for t in turns.values()),
        "turns": [
            {
                "turn_id": t["turn_id"],
                "calls": t["calls"],
                "cost": None if t["cost"] is None else str(t["cost"]),
                "tokens": t["usage"].as_json(),
                "at": t.get("at"),
            }
            for t in turns.values()
        ],
    }
