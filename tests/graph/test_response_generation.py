from decimal import Decimal

import pytest

from wallet_agent.domain.models import Asset, TokenBalance, TokenPrice
from wallet_agent.graph import build_graph
from wallet_agent.models import RouteDecision
from wallet_agent.models.contracts import AgentResponseDraft

WALLET = "0x" + "1" * 40
USDC = "0x" + "2" * 40


class ResponseModel:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def classify(self, _request):
        return RouteDecision(intent="transfer")

    async def extract(self, _kind, _request):
        return {"chain": "ETH", "symbol": "USDC", "amount": "1"}

    async def respond(self, request, facts):
        self.calls.append((request, facts))
        if self.error:
            raise self.error
        return self.response


class SuggestionAwareSwapModel(ResponseModel):
    async def classify(self, _request):
        return RouteDecision(intent="swap_quote")

    async def extract(self, _kind, request):
        assert request["suggestion_data"] == {
            "intent": "swap_quote",
            "source_symbol": "USDC",
            "destination_symbol": "ETH",
            "input_amount": "1",
        }
        return request["suggestion_data"]


class Wallet:
    chain_index_by_name = {"ETH": "1", "BASE": "8453"}

    async def get_token_balances(self, _address, chain_indexes):
        assert chain_indexes == ["1", "8453"]
        return [
            TokenBalance(
                asset=Asset(chain="ETH", chain_id=1, symbol="USDC", decimals=6, address=USDC),
                amount=Decimal("12.5"),
                amount_raw="12500000",
            )
        ]


class Catalog:
    async def list_assets(self, _query):
        return [
            Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18),
            Asset(chain="ETH", chain_id=1, symbol="USDC", decimals=6, address=USDC),
        ]


class Prices:
    async def get_prices(self, assets):
        return [TokenPrice(asset=asset, usd_price=Decimal("1"), provider="okx") for asset in assets]


def turn(message):
    return {
        "conversation_id": "response-test",
        "user_id": "test-user",
        "request": {"message": message, "address": WALLET, "chain": "ETH"},
        "wallet_context": {
            "address": WALLET,
            "chain": "ETH",
            "chain_id": "1",
            "native_symbol": "ETH",
        },
    }


@pytest.mark.asyncio
async def test_transfer_clarification_uses_model_message_and_never_exposes_internal_field_names():
    model = ResponseModel(
        AgentResponseDraft(
            language="en",
            message="Who should receive the 1 USDC transfer?",
            suggestions=[
                {
                    "label": "Use another recipient",
                    "message": "Send 1 USDC to 0x2222",
                    "data": {"intent": "transfer", "amount": "1", "chain": "ETH"},
                }
            ],
        )
    )
    graph = build_graph(model=model, wallet_provider=Wallet())

    result = await graph.ainvoke(
        turn("Send 1 USDC"),
        config={"configurable": {"thread_id": "response-transfer-en"}},
    )

    response = result["response"]
    assert response["message"] == "Who should receive the 1 USDC transfer?"
    assert "transfer_recipient" not in response["message"]
    assert response["suggestions"][0]["data"] == {
        "intent": "transfer",
        "amount": "1",
        "chain": "ETH",
        "symbol": "USDC",
    }


@pytest.mark.asyncio
async def test_open_swap_clarification_supplies_balance_chain_and_price_facts_to_model():
    class SwapResponseModel(ResponseModel):
        async def classify(self, _request):
            return RouteDecision(intent="swap_quote")

        async def extract(self, _kind, _request):
            return {}

    model = SwapResponseModel(
        AgentResponseDraft(
            language="en",
            message="You have 12.5 USDC on Ethereum. What would you like to receive?",
            suggestions=[
                {
                    "label": "Swap 10 USDC to ETH",
                    "message": "Swap 10 USDC to ETH",
                    "data": {
                        "intent": "swap_quote",
                        "source_chain": "ETH",
                        "destination_chain": "ETH",
                        "source_symbol": "USDC",
                        "destination_symbol": "ETH",
                        "input_amount": "10",
                        "amount_mode": "exact_in",
                    },
                }
            ],
        )
    )
    graph = build_graph(
        model=model,
        wallet_provider=Wallet(),
        providers=[Catalog()],
        price_provider=Prices(),
    )

    result = await graph.ainvoke(
        {
            **turn("I want to swap"),
            "request": {"message": "I want to swap", "address": WALLET, "chain": "ETH"},
        },
        config={"configurable": {"thread_id": "response-open-swap"}},
    )

    assert result["response"]["message"].startswith("You have 12.5 USDC")
    assert result["response"]["suggestions"]
    assert result["response"]["suggestions"][0]["data"]
    assert result["response"]["suggestions"][0]["data"]["intent"] == "swap_quote"
    facts = model.calls[0][1]
    assert facts["supported_chains"] == ["BASE", "ETH"]
    assert facts["balances"][0]["symbol"] == "USDC"
    assert facts["prices"][0]["usd_price"] == "1"


@pytest.mark.asyncio
async def test_response_model_failure_uses_safe_non_field_name_fallback():
    graph = build_graph(
        model=ResponseModel(error=RuntimeError("model unavailable")),
        wallet_provider=Wallet(),
    )

    result = await graph.ainvoke(
        turn("Send 1 USDC"),
        config={"configurable": {"thread_id": "response-fallback"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert "transfer_recipient" not in result["response"]["message"]
    assert result["response"]["message"]


@pytest.mark.asyncio
async def test_suggestion_metadata_is_whitelisted_before_slot_extraction():
    model = SuggestionAwareSwapModel(
        AgentResponseDraft(language="en", message="Choose a swap", suggestions=[])
    )
    graph = build_graph(model=model, wallet_provider=Wallet(), providers=[Catalog()])
    result = await graph.ainvoke(
        {
            **turn("Swap"),
            "request": {
                "message": "Swap",
                "address": WALLET,
                "chain": "ETH",
                "metadata": {
                    "suggestion_data": {
                        "intent": "swap_quote",
                        "source_symbol": "USDC",
                        "destination_symbol": "ETH",
                        "input_amount": "1",
                        "private_key": "must-not-leak",
                    }
                },
            },
        },
        config={"configurable": {"thread_id": "suggestion-whitelist"}},
    )
    assert result["active_task"]["slots"]["source_symbol"] == "USDC"
    assert "private_key" not in result["active_task"]["slots"]
