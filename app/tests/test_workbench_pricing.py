"""用了多少、花了多少（app/domain/workbench/pricing.py）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.workbench.pricing import (
    DEFAULT_PRICES,
    Usage,
    call_of,
    cost_of,
    parse_prices,
    price_view,
    usage_of,
)
from core.config import Settings

KIMI = DEFAULT_PRICES.of("kimi-k3")
# 真的 Codex 0.155.1 报的一条用量（磁带里录下来的）
REAL = {
    "total": {
        "totalTokens": 44908,
        "inputTokens": 43904,
        "cachedInputTokens": 41216,
        "cacheWriteInputTokens": 2048,
        "outputTokens": 1004,
        "reasoningOutputTokens": 416,
    },
    "last": {
        "totalTokens": 8161,
        "inputTokens": 8064,
        "cachedInputTokens": 7424,
        "cacheWriteInputTokens": 512,
        "outputTokens": 97,
        "reasoningOutputTokens": 32,
    },
    "modelContextWindow": 258400,
}


def test_the_published_price_of_kimi_k3():
    assert KIMI is not None
    assert (KIMI.input, KIMI.cached_input, KIMI.cache_write, KIMI.output) == (
        Decimal("20"),
        Decimal("2"),
        Decimal("20"),
        Decimal("100"),
    )
    assert DEFAULT_PRICES.currency == "CNY" and DEFAULT_PRICES.as_of == "2026-10-04"
    assert "platform.kimi.com" in KIMI.source


def test_the_input_reported_by_codex_is_split_into_three():
    """它的「输入」里含命中缓存的和写进缓存的：拆开，四项互不重叠。"""
    last = call_of(REAL)
    assert last == Usage(input=128, cached_input=7424, cache_write=512, output=97)
    assert last.total == 8161  # 和它自己报的总数对得上
    assert usage_of(REAL["total"]).total == 44908


def test_one_call_costs_what_the_price_list_says():
    # 128×20 + 7424×2 + 512×20 + 97×100 = 37348，每百万
    assert cost_of(call_of(REAL), KIMI) == Decimal("0.037348")
    # 整条线到这时一共 0.236592；老的算法（累计当一步、每千 token 0.01）会记成 0.449
    assert cost_of(usage_of(REAL["total"]), KIMI) == Decimal("0.236592")
    assert cost_of(Usage(), KIMI) == Decimal("0")


def test_calls_add_up():
    one = Usage(input=100, cached_input=1000, cache_write=10, output=20)
    two = Usage(input=1, cached_input=2, cache_write=3, output=4)
    assert one + two == Usage(101, 1002, 13, 24)
    assert cost_of(one + two, KIMI) == cost_of(one, KIMI) + cost_of(two, KIMI)
    assert (one + two).as_json()["total"] == 1140


def test_a_model_without_a_price_gets_no_amount():
    assert cost_of(call_of(REAL), DEFAULT_PRICES.of("some-new-model")) is None
    assert cost_of(call_of(REAL), DEFAULT_PRICES.of(None)) is None
    shown = price_view(DEFAULT_PRICES, "some-new-model")
    assert shown["priced"] is False and shown["per_million"] is None
    assert price_view(DEFAULT_PRICES, "kimi-k3") == {
        "model": "kimi-k3",
        "priced": True,
        "currency": "CNY",
        "as_of": "2026-10-04",
        "per_million": {
            "input": "20",
            "cached_input": "2",
            "cache_write": "20",
            "output": "100",
        },
        "source": "https://platform.kimi.com/docs/pricing/chat-k3",
        "estimated": True,
    }


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (None, Usage()),
        ({}, Usage()),
        ("x", Usage()),
        ({"last": None}, Usage()),
        # 只有累计、没有「这一次」：当作没有，不拿累计顶替
        ({"total": {"totalTokens": 5000}}, Usage()),
        # 只报了一个总数：按输入算，不往多里估
        ({"last": {"totalTokens": 1000}}, Usage(input=1000)),
        # 报的数自相矛盾：不出负数
        (
            {"last": {"inputTokens": 10, "cachedInputTokens": 50, "outputTokens": -3}},
            Usage(input=0, cached_input=10, cache_write=0, output=0),
        ),
    ],
)
def test_odd_reports(reported, expected):
    assert call_of(reported) == expected


def test_the_price_list_comes_from_configuration():
    custom = parse_prices(
        '{"as_of": "2027-01-01", "currency": "CNY", "models": {"m": '
        '{"input": "1", "cached_input": "0.1", "cache_write": "1.25", "output": "4"}}}'
    )
    assert cost_of(
        Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000), custom.of("m")
    ) == (Decimal("6.350000"))
    assert Settings(_env_file=None).workbench_model_prices().of("kimi-k3") == KIMI
    changed = Settings(
        _env_file=None,
        WORKBENCH_MODEL_PRICES_JSON='{"models": {"kimi-k3": {"input": "1",'
        ' "cached_input": "1", "cache_write": "1", "output": "1"}}}',
    )
    assert changed.workbench_model_prices().of("kimi-k3").output == Decimal("1")


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        '{"models": []}',
        '{"models": {"m": {"input": "1"}}}',
        '{"models": {"m": {"input": "x", "cached_input": "1", "cache_write": "1", "output": "1"}}}',
        '{"models": {"m": {"input": "-1", "cached_input": "1", "cache_write": "1", "output": "1"}}}',
        '{"models": {"m": "20"}}',
    ],
)
def test_a_wrong_price_list_stops_the_start(bad):
    with pytest.raises(ValueError):
        parse_prices(bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None, WORKBENCH_MODEL_PRICES_JSON=bad)


# ---------------- 一条通知带来多少新用量 ----------------
def _report(last: int, total: int) -> dict:
    def one(n: int) -> dict:
        return {"inputTokens": n, "cachedInputTokens": 0, "outputTokens": 0}

    return {"last": one(last), "total": one(total)}


def test_the_new_usage_is_what_the_running_total_grew_by():
    from app.domain.workbench.pricing import cumulative_of, fresh_usage

    first = _report(100, 100)
    # 这条线第一次见：不知道上一次的累计，用「这一次调用」
    assert fresh_usage(first, None).total == 100
    seen = cumulative_of(first)
    # 平常：累计多出来的就是这一次
    assert fresh_usage(_report(50, 150), seen).total == 50
    # 原样再报一遍：没有新用量
    assert fresh_usage(first, seen).total == 0
    # 中间漏了一条（断线）：按累计补上，不只算最后这一次
    assert fresh_usage(_report(30, 260), seen).total == 160
    # 累计变小了（不是同一个累计）：退回用这一次
    assert fresh_usage(_report(40, 60), seen).total == 40
    # 没报累计：用这一次
    assert fresh_usage({"last": {"inputTokens": 7}}, seen).total == 7
    assert cumulative_of({"last": {}}) is None and cumulative_of(None) is None


def test_usage_since():
    assert Usage(5, 4, 3, 2).since(Usage(1, 1, 1, 1)) == Usage(4, 3, 2, 1)
    assert Usage(5, 0, 0, 0).since(Usage(6, 0, 0, 0)) is None
    assert Usage.from_json({"input": 3, "output": 2, "total": 5}) == Usage(3, 0, 0, 2)
    assert Usage.from_json(None) is None


def test_complete_responses_usage_accounts_for_cache_and_reasoning_once():
    meter = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
        "output_tokens_details": {"reasoning_tokens": 4},
    }
    assert Usage.from_responses(meter) == Usage(5, 3, 2, 5)
    for changes in (
        {"total_tokens": 14},
        {"input_tokens": True},
        {"output_tokens": -1},
        {"input_tokens_details": {"cached_tokens": 11}},
        {"output_tokens_details": {"reasoning_tokens": 6}},
        {"input_tokens_details": {"audio_tokens": 2}},
        {"input_tokens_details": None},
    ):
        with pytest.raises(ValueError):
            Usage.from_responses(meter | changes)
    with pytest.raises(ValueError):
        Usage.from_responses({})
