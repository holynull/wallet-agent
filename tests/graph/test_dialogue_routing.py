import pytest

from wallet_agent.domain.models import Asset, TokenBalance
from wallet_agent.graph import build_graph
from wallet_agent.graph.dialogue import resolve_quote_reference, resolve_read_reference
from wallet_agent.graph.nodes import GraphRuntime, make_nodes
from wallet_agent.models import RouteDecision

WALLET = "0x" + "1" * 40


class DialogueModel:
    def __init__(self, intents):
        self.intents = iter(intents)
        self.requests = []

    async def classify(self, request):
        self.requests.append(request)
        return RouteDecision(intent=next(self.intents))


class WalletProvider:
    chain_index_by_name = {"ETH": "1"}

    async def get_token_balances(self, _address, chain_indexes):
        assert chain_indexes == ["1"]
        return [
            TokenBalance(
                asset=Asset(chain="ETH", symbol="USDC", decimals=6, address="0x" + "2" * 40),
                amount="2",
                amount_raw="2000000",
            )
        ]


def turn(message):
    return {
        "request": {"message": message, "address": WALLET, "chain": "ETH"},
        "wallet_context": {"address": WALLET, "chain": "ETH"},
    }


@pytest.mark.asyncio
async def test_dialogue_manager_persists_topic_and_referent_for_followup_turns():
    graph = build_graph(
        model=DialogueModel(["wallet_query", "clarification"]),
        wallet_provider=WalletProvider(),
    )
    config = {"configurable": {"thread_id": "dialogue-routing"}}

    first = await graph.ainvoke(turn("我的余额"), config=config)
    second = await graph.ainvoke(
        {
            **turn("哪一项？"),
            "conversation_history": [
                {"role": "user", "content": "我的余额"},
                {
                    "role": "assistant",
                    "content": first["response"]["message"],
                    "artifacts": {"response": first["response"]},
                },
                {"role": "user", "content": "哪一项？"},
            ],
        },
        config=config,
    )

    assert second["dialogue"]["active_topic"] == "wallet"
    assert second["dialogue"]["last_result_kind"] == "wallet_query"
    assert second["response"]["kind"] == "wallet_query"


@pytest.mark.asyncio
async def test_supervisor_receives_dialogue_snapshot_and_bounded_history():
    model = DialogueModel(["clarification"])
    graph = build_graph(model=model, wallet_provider=WalletProvider())
    history = [
        {"role": "user", "content": f"turn {index}"}
        for index in range(30)
    ]

    result = await graph.ainvoke(
        {
            "request": {"message": "继续"},
            "conversation_history": history,
        },
        config={"configurable": {"thread_id": "dialogue-model-context"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert model.requests
    request = model.requests[0]
    assert request["dialogue"]["active_topic"] == "unknown"
    assert len(request["conversation_history"]) == 12
    assert request["conversation_history"] == history[-12:]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["我的全部余额", "我全钱包里全部余额是多少", "所有资产有多少"])
async def test_balance_request_is_classified_even_after_preflight_failure(message):
    """A new balance request must not be hijacked by stale transaction history."""
    model = DialogueModel(["wallet_query"])
    graph = build_graph(model=model, wallet_provider=WalletProvider())
    result = await graph.ainvoke(
        {
            "request": {"message": message, "address": WALLET, "chain": "ETH"},
            "wallet_context": {"address": WALLET, "chain": "ETH"},
            "conversation_history": [
                {
                    "role": "assistant",
                    "content": "交易预检查未通过",
                    "artifacts": {
                        "response": {
                            "kind": "error",
                            "errors": [
                                {
                                    "code": "TRANSACTION_PREFLIGHT_FAILED",
                                    "details": {
                                        "preflight": {
                                            "chain": "BSC",
                                            "checks": [{"code": "INSUFFICIENT_BALANCE"}],
                                        }
                                    },
                                }
                            ],
                        }
                    },
                }
            ],
        },
        config={"configurable": {"thread_id": f"balance-after-preflight-{message}"}},
    )

    assert model.requests, "ordinary business requests must reach the model router"
    assert result["intent"] == "wallet_query"
    assert result["response"]["kind"] == "wallet_query"
    assert result["supervisor_decision"]["source"] == "model"
    assert model.requests[0]["previous_preflight"]["chain"] == "BSC"


@pytest.mark.asyncio
async def test_model_route_wins_over_previous_read_topic_for_a_new_request():
    model = DialogueModel(["price_query"])
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {
            "request": {"message": "ETH 现在多少钱", "chain": "ETH"},
            "conversation_history": [
                {
                    "role": "assistant",
                    "content": "当前钱包有 USDC。",
                    "artifacts": {
                        "response": {
                            "kind": "wallet_query",
                            "wallet": {"chain": "ETH", "token_balances": []},
                        }
                    },
                }
            ],
        },
        config={"configurable": {"thread_id": "model-route-new-topic"}},
    )

    assert model.requests
    assert result["intent"] == "price_query"
    assert result["supervisor_decision"]["source"] == "model"


@pytest.mark.asyncio
async def test_invalid_model_intent_falls_back_to_safe_clarification():
    class InvalidModel:
        async def classify(self, _request):
            return {"intent": "execute_arbitrary_node"}

    result = await build_graph(model=InvalidModel()).ainvoke(
        {"request": {"message": "帮我转 1 ETH"}},
        config={"configurable": {"thread_id": "invalid-model-route"}},
    )

    assert result["intent"] == "clarification"
    assert result["supervisor_decision"]["error"]["code"] == "SUPERVISOR_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_read_terminal_fallback_has_a_natural_language_message():
    nodes = make_nodes(
        GraphRuntime(model=DialogueModel(["swap_quote"]), providers={}, chains={})
    )

    result = await nodes["response"](
        {"intent": "swap_quote", "request": {"message": "给我一个兑换报价"}}
    )

    assert result["response"]["kind"] == "swap_quote"
    assert result["response"]["message"]


def test_price_followup_reuses_chain_and_replaces_symbol():
    dialogue = {
        "active_topic": "price",
        "referent": {
            "kind": "asset",
            "id": "ETH:BTC",
            "facts": {"chain": "ETH", "symbol": "BTC", "usd_price": "60000"},
        },
    }

    result = resolve_read_reference("那 ETH 呢？", dialogue)

    assert result == {
        "intent": "price_query",
        "referent": {"kind": "asset", "id": "ETH:BTC"},
        "arguments": {"chain": "ETH", "symbol": "ETH"},
    }


def test_transaction_followup_reuses_only_explicit_previous_hash_and_chain():
    dialogue = {
        "active_topic": "transaction",
        "referent": {
            "kind": "transaction",
            "id": "BASE:0xabc",
            "facts": {"chain": "BASE", "tx_hash": "0xabc", "status": "pending"},
        },
    }

    result = resolve_read_reference("刚才那笔交易到账了吗？", dialogue)

    assert result == {
        "intent": "transaction_status",
        "referent": {"kind": "transaction", "id": "BASE:0xabc"},
        "arguments": {"chain": "BASE", "tx_hash": "0xabc"},
    }


def test_quote_comparison_uses_only_displayed_candidates():
    referent = {
        "kind": "quote",
        "id": "quote-set",
        "facts": {"quotes": [{"provider": "a"}, {"provider": "b"}]},
    }

    result = resolve_read_reference("哪个报价更划算？", {"referent": referent})

    assert result == {
        "intent": "quote_compare",
        "referent": {"kind": "quote", "id": "quote-set"},
        "arguments": {"quotes": [{"provider": "a"}, {"provider": "b"}]},
    }


def test_quote_ordinal_selects_displayed_candidate_without_execution():
    referent = {
        "kind": "quote",
        "id": "quote-set",
        "facts": {"quotes": [{"provider": "a"}, {"provider": "b"}]},
    }

    result = resolve_quote_reference("第二个", referent)

    assert result == {
        "intent": "swap_quote",
        "referent": {"kind": "quote", "id": "quote-set"},
        "arguments": {"selected_quote": {"provider": "b"}},
        "requires_confirmation": True,
    }
