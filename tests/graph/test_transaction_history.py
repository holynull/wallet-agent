"""Design v1.6.9 history card: recent transactions, direction filter and summary."""

import re

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.domain.models import TransactionHistoryPage, TransactionRecord, TransactionStatus
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import (
    _history_direction,
    _history_targets,
    _transaction_history_item,
    _transaction_history_payload,
)

EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"
BTC = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
TRON = "TNwXHkvCwj3nayWPbFbxD7rBPncBsyBS27"

CONTEXT = {
    "address": EVM,
    "chain": "ETH",
    "wallet_accounts": [
        {"chain": "ETH", "address": EVM},
        {"chain": "BSC", "address": EVM},
        {"chain": "BTC", "address": BTC},
        {"chain": "TRON", "address": TRON},
    ],
}


def _record(**overrides):
    payload = {
        "chain": "BSC",
        "tx_hash": "0xaa",
        "status": TransactionStatus.CONFIRMED,
        "from_address": EVM,
        "to_address": "0xdead",
        "value": "120300000000000000",
        "symbol": "BNB",
        "decimals": 18,
        "occurred_at": "2025-07-17T09:35:01Z",
    }
    payload.update(overrides)
    return TransactionRecord(**payload)


def test_history_targets_cover_declared_accounts_and_chain_filter():
    assert _history_targets(CONTEXT) == [
        ("ETH", EVM),
        ("BSC", EVM),
        ("BTC", BTC),
        ("TRON", TRON),
    ]
    assert _history_targets(CONTEXT, "BSC") == [("BSC", EVM)]


def test_direction_is_derived_from_from_and_to():
    assert _history_direction({"to_address": EVM, "from_address": "0xdead"}, EVM) == "receive"
    assert _history_direction({"to_address": "0xdead", "from_address": EVM}, EVM) == "send"
    assert _history_direction({"to_address": "0xdead", "from_address": "0xbeef"}, EVM) == "unknown"
    # A provider that declares the kind wins over the from/to guess.
    assert (
        _history_direction(
            {"direction": "swap", "to_address": "0xdead", "from_address": EVM}, EVM
        )
        == "swap"
    )


def test_item_exposes_the_fields_the_card_renders():
    item = _transaction_history_item(_record(), EVM)

    assert item["direction"] == "send"
    assert item["counterparty"] == "0xdead"
    assert item["symbol"] == "BNB"
    assert item["amount_raw"] == "120300000000000000"
    assert item["occurred_at"] == "2025-07-17T09:35:01Z"
    # Native coin (BNB) reuses its chain logo; the chain logo is always exposed.
    assert "binance/info/logo.png" in item["chain_logo_url"]
    assert item["logo_url"] == item["chain_logo_url"]

    received = _transaction_history_item(
        _record(tx_hash="0xbb", to_address=EVM, from_address="0xfeed"), EVM
    )
    assert received["direction"] == "receive"
    assert received["counterparty"] == "0xfeed"


def test_item_keeps_a_provider_logo_for_non_native_tokens():
    item = _transaction_history_item(
        _record(symbol="USDC", logo_url="https://static.okx.com/usdc.png"), EVM
    )
    assert item["logo_url"] == "https://static.okx.com/usdc.png"
    assert "binance/info/logo.png" in item["chain_logo_url"]


def test_payload_sorts_by_time_and_summarises_each_direction():
    items = [
        _transaction_history_item(_record(), EVM),
        _transaction_history_item(
            _record(
                tx_hash="0xbb",
                to_address=EVM,
                from_address="0xfeed",
                occurred_at="2025-07-18T09:35:01Z",
            ),
            EVM,
        ),
        _transaction_history_item(
            _record(tx_hash="0xcc", direction="swap", occurred_at="2025-07-16T09:35:01Z"), EVM
        ),
    ]

    payload = _transaction_history_payload(items, limit=10)

    assert [item["tx_hash"] for item in payload["items"]] == ["0xbb", "0xaa", "0xcc"]
    assert payload["total"] == 3
    assert payload["summary"] == "1 笔转出、1 笔入账、1 笔兑换"
    assert "3 笔记录拉出来了" in payload["message"]
    # All fetched rows are shown, so no "(共 N 笔)" disambiguation is needed.
    assert "共" not in payload["message"]
    assert "筛选只看支出" in payload["message"]


def test_payload_direction_filter_keeps_the_other_counts():
    items = [
        _transaction_history_item(_record(), EVM),
        _transaction_history_item(
            _record(tx_hash="0xbb", to_address=EVM, from_address="0xfeed"), EVM
        ),
    ]

    payload = _transaction_history_payload(items, direction="send", limit=10)

    assert payload["total"] == 1
    assert [item["tx_hash"] for item in payload["items"]] == ["0xaa"]
    assert payload["filter"]["direction"] == "send"
    # Under a direction filter the summary counts the rows the card shows.
    assert payload["summary"] == "1 笔转出"


def test_payload_summary_and_prose_do_not_contradict_the_page_size():
    """Regression: "最近 20 笔" must not sit next to an all-128 summary."""
    items = []
    for index in range(60):
        items.append(
            _transaction_history_item(
                _record(
                    tx_hash=f"0x{index:02x}" * 4,
                    direction="send" if index % 2 else "receive",
                    occurred_at=f"2025-07-{1 + (index % 28):02d}T09:35:01Z",
                ),
                EVM,
            )
        )

    payload = _transaction_history_payload(items, limit=20)

    assert payload["total"] == 20
    assert payload["fetched"] == 60
    summary_counts = [int(part) for part in re.findall(r"(\d+) 笔", payload["summary"])]
    assert sum(summary_counts) == 20, "summary must count only the 20 shown rows"
    # The “共 N 笔” note must describe the shown rows (20), never the larger
    # fetched set (60), or it fights the card the user actually sees.
    assert "共 20 笔" in payload["message"]
    assert "共 60 笔" not in payload["message"]
    assert "最近 20 笔" in payload["message"]


def test_native_rows_without_decimals_inherit_the_chain_scale():
    """OKX history rows omit decimals for native coins; fill them from the
    chain so the model can convert the raw amount (Lif's history-card report:
    BTC/TRX rows showed “数量未返回/最小单位”)."""
    btc = _transaction_history_item(
        _record(chain="BTC", symbol="BTC", decimals=None, value="123000000"), BTC
    )
    assert btc["decimals"] == 8
    assert btc["raw_only"] is False

    tron = _transaction_history_item(
        _record(chain="TRON", symbol="TRX", decimals=None, value="1500000"), TRON
    )
    assert tron["decimals"] == 6
    assert tron["raw_only"] is False


def test_non_native_token_without_decimals_stays_raw_only():
    item = _transaction_history_item(
        _record(chain="BSC", symbol="USDC", decimals=None), EVM
    )
    # Without a decimals hint we must not guess a scale (USDC is 18 on BSC but
    # the model must not assume it), so it stays raw_only.
    assert item["decimals"] is None
    assert item["raw_only"] is True


def test_payload_handles_an_empty_history():
    payload = _transaction_history_payload([], limit=10)

    assert payload["ok"] is True
    assert payload["items"] == []
    assert payload["message"] == "没有查到符合条件的交易记录。"


class FakeExplorer:
    def __init__(self, by_address):
        self.by_address = by_address
        self.calls = []

    async def get_transaction_history(self, address, chain=None, *, limit=20):
        self.calls.append((address, chain, limit))
        return TransactionHistoryPage(
            transactions=[_record(**item) for item in self.by_address.get(address, [])]
        )


class FlakyExplorer:
    async def get_transaction_history(self, address, chain=None, *, limit=20):
        raise RuntimeError("explorer down")


class HistoryAdapter:
    """Adapter fallback used when the explorer is unavailable."""

    async def get_transaction_history(self, address, *, limit=20):
        return [_record(tx_hash="0xada", chain="BTC", symbol="BTC", decimals=8)]


class HistoryModel:
    def __init__(self, args):
        self.args = args

    async def invoke_react(self, messages, tools):
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[{"id": "hist-1", "name": "transaction_history", "args": self.args}],
            )
        return AIMessage(content="最近交易如下 👆")


async def _run(args):
    explorer = FakeExplorer(
        {
            EVM: [
                {"tx_hash": "0xaa"},
                {"tx_hash": "0xbb", "to_address": EVM, "from_address": "0xfeed"},
            ]
        }
    )
    graph = build_graph(model=HistoryModel(args), explorer_provider=explorer)
    result = await graph.ainvoke(
        {
            "request": {"message": "我最近有哪些交易？", "address": EVM, "chain": "ETH"},
            "wallet_context": CONTEXT,
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": f"history-{args}"}},
    )
    return result, explorer


@pytest.mark.asyncio
async def test_graph_returns_the_history_card_and_honours_the_direction_filter():
    result, explorer = await _run({"direction": "send"})

    assert result["response"]["kind"] == "react"
    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages
    assert '"direction": "send"' in tool_messages[0].content
    # OKX rejects an unscoped query, so each declared chain is asked separately.
    assert (EVM, "ETH", 10) in explorer.calls
    assert (EVM, "BSC", 10) in explorer.calls
    assert not any(chain is None for _, chain, _ in explorer.calls)


@pytest.mark.asyncio
async def test_history_falls_back_to_chain_adapters_when_the_explorer_fails():
    graph = build_graph(
        model=HistoryModel({"chain": "BTC"}),
        explorer_provider=FlakyExplorer(),
        chains={"BTC": HistoryAdapter()},
    )

    result = await graph.ainvoke(
        {
            "request": {"message": "我最近有哪些交易？", "address": EVM, "chain": "ETH"},
            "wallet_context": CONTEXT,
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "history-fallback"}},
    )

    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages
    assert "0xada" in tool_messages[0].content
    # The adapter covered the gap, so nothing is reported as missing.
    assert "partial_errors" not in tool_messages[0].content


@pytest.mark.asyncio
async def test_unreachable_chain_is_reported_instead_of_looking_empty():
    """A chain we cannot read must not be presented as "no transactions"."""
    graph = build_graph(model=HistoryModel({"chain": "ETH"}), explorer_provider=FlakyExplorer())

    result = await graph.ainvoke(
        {
            "request": {"message": "ETH 最近有哪些交易？", "address": EVM, "chain": "ETH"},
            "wallet_context": CONTEXT,
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "history-unreachable"}},
    )

    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages
    assert "ETH:explorer" in tool_messages[0].content
