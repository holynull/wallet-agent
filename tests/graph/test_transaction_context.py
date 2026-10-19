"""The App can open the assistant from a transaction detail screen with a default question."""

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from wallet_agent.graph.build import build_graph

EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"
TX_HASH = "0x" + "a" * 64


class QuietModel:
    async def invoke_react(self, messages, tools):
        return AIMessage(content="好的。")


@pytest.mark.asyncio
async def test_transaction_context_reaches_the_model_prompt():
    graph = build_graph(model=QuietModel())
    result = await graph.ainvoke(
        {
            "request": {
                "message": "解释失败原因？",
                "address": EVM,
                "chain": "ETH",
                "transaction_query": {"chain": "ETH", "tx_hash": TX_HASH},
            },
            "transaction_query": {"chain": "ETH", "tx_hash": TX_HASH},
            "wallet_context": {"address": EVM, "chain": "ETH"},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "tx-context"}},
    )

    system = next(
        item for item in result["messages"] if isinstance(item, SystemMessage)
    )
    assert TX_HASH in str(system.content)
    assert "explain" not in str(system.content)
    assert "transaction_status" in str(system.content)


@pytest.mark.asyncio
async def test_no_transaction_context_keeps_the_prompt_clean():
    graph = build_graph(model=QuietModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "你好", "address": EVM, "chain": "ETH"},
            "wallet_context": {"address": EVM, "chain": "ETH"},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "tx-context-none"}},
    )

    system = next(
        item for item in result["messages"] if isinstance(item, SystemMessage)
    )
    assert "当前交易上下文" not in str(system.content)


class RecordingReactModel:
    def __init__(self):
        self.calls = []

    async def invoke_react(self, messages, tools):
        self.calls.append([tool.name for tool in tools])
        return AIMessage(content="订单还在处理中，建议稍后再查。")


@pytest.mark.asyncio
async def test_swap_order_context_reaches_the_model_prompt():
    """A "为什么还在处理中？" opened from the swap order detail page carries
    the order id into the model prompt so it can query the order."""
    graph = build_graph(model=QuietModel())
    result = await graph.ainvoke(
        {
            "request": {
                "message": "为什么还在处理中？",
                "address": EVM,
                "chain": "ETH",
                "swap_query": {"provider_order_id": "order-123", "tx_hash": TX_HASH},
            },
            "swap_query": {"provider_order_id": "order-123", "tx_hash": TX_HASH},
            "wallet_context": {"address": EVM, "chain": "ETH"},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "swap-order-context"}},
    )

    system = next(
        item for item in result["messages"] if isinstance(item, SystemMessage)
    )
    assert "order-123" in str(system.content)
    assert TX_HASH in str(system.content)
    assert "query_swap_status" in str(system.content)


@pytest.mark.asyncio
async def test_swap_order_context_hides_transaction_history_and_carries_chain():
    """Opening the assistant from a swap order detail page must scope the ReAct
    toolset (no recent-transaction history card over the order analysis) and
    surface the order chain so the model stops guessing ETH/BTC first."""
    model = RecordingReactModel()
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {
            "request": {
                "message": "分析这笔订单的详情？",
                "address": EVM,
                "chain": "BSC",
                "swap_query": {
                    "provider_order_id": "order-456",
                    "tx_hash": TX_HASH,
                    "chain": "BSC",
                },
            },
            "swap_query": {
                "provider_order_id": "order-456",
                "tx_hash": TX_HASH,
                "chain": "BSC",
            },
            "wallet_context": {"address": EVM, "chain": "BSC"},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "swap-order-context-scoped"}},
    )

    system = next(
        item for item in result["messages"] if isinstance(item, SystemMessage)
    )
    assert "订单链 BSC" in str(system.content)
    assert "不要调用 transaction_history" in str(system.content)
    # The model never sees the recent-transaction tool in this order-scoped turn.
    assert model.calls and "transaction_history" not in model.calls[0]

