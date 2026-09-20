"""Deterministic and opt-in model evaluations for core wallet capabilities."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Any

from wallet_agent.domain.models import (
    Asset,
    AssetQuery,
    FeeEstimate,
    NormalizedQuote,
    TokenBalance,
    TokenPrice,
    TransactionStatus,
    UnsignedTransaction,
)
from wallet_agent.graph import build_graph

WALLET_ADDRESS = "0x" + "1" * 40
RECIPIENT_ADDRESS = "0x" + "2" * 40
USDC_ADDRESS = "0x" + "3" * 40
USDT_ADDRESS = "0x" + "4" * 40


@dataclass(frozen=True)
class EvalCase:
    id: str
    capability: str
    turns: tuple[str, ...]
    offline_outputs: tuple[dict[str, Any], ...]
    expected: dict[str, Any]
    dimensions: tuple[str, ...] = ()


class FixedOutputModel:
    """Return known structured outputs while exercising the real graph."""

    def __init__(self, outputs: Sequence[Mapping[str, Any]]) -> None:
        self._outputs = [dict(item) for item in outputs]
        self._index = 0

    async def ainvoke(self, _request: Any) -> dict[str, Any]:
        if not self._outputs:
            raise RuntimeError("fixed model has no output")
        index = min(self._index, len(self._outputs) - 1)
        self._index += 1
        return dict(self._outputs[index])


class FakeChainAdapter:
    """Read-only chain simulator with deterministic Base balances and fees."""

    chain = "BASE"

    def __init__(self) -> None:
        self.broadcast_calls = 0
        self.native_asset = Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18)
        self.usdc_asset = Asset(
            chain="BASE",
            chain_id=8453,
            symbol="USDC",
            decimals=6,
            address=USDC_ADDRESS,
        )

    async def validate_address(self, address: str) -> bool:
        return address.startswith("0x") and len(address) == 42

    async def get_native_balance(self, _address: str) -> TokenBalance:
        return TokenBalance(
            asset=self.native_asset,
            amount=Decimal("2"),
            amount_raw="2000000000000000000",
        )

    async def get_token_balances(self, _address: str) -> list[TokenBalance]:
        return [
            TokenBalance(
                asset=self.usdc_asset,
                amount=Decimal("25"),
                amount_raw="25000000",
            )
        ]

    async def get_token_balance(self, _asset: Asset, _owner: str) -> TokenBalance:
        return (await self.get_token_balances(_owner))[0]

    async def estimate_fee(self, *, to: str | None = None, data: str | None = None) -> FeeEstimate:
        del to, data
        return FeeEstimate(
            chain="BASE",
            chain_id=8453,
            asset=self.native_asset,
            amount=Decimal("0.0001"),
            amount_raw="100000000000000",
            gas_limit="21000",
        )

    def build_native_transfer(
        self, *, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction:
        del from_address
        return UnsignedTransaction(
            chain="BASE",
            chain_id=8453,
            to=to_address,
            data="0x",
            value=amount_raw,
        )

    def build_erc20_transfer(
        self, *, token: Asset, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction:
        del from_address, to_address, amount_raw
        return UnsignedTransaction(
            chain="BASE",
            chain_id=8453,
            to=str(token.address),
            data="0xa9059cbb",
            value="0",
        )

    async def get_transaction_status(self, _tx_hash: str) -> TransactionStatus:
        return TransactionStatus.CONFIRMED


class FakeSwapProvider:
    """Quote simulator that records every action capable of advancing a swap."""

    provider_name = "bridgers"

    def __init__(self) -> None:
        self.quote_calls = 0
        self.prepare_calls = 0
        self.broadcast_calls = 0
        self.assets = {
            ("BASE", "USDC"): Asset(
                chain="BASE",
                chain_id=8453,
                symbol="USDC",
                decimals=6,
                address=USDC_ADDRESS,
            ),
            ("BASE", "USDT"): Asset(
                chain="BASE",
                chain_id=8453,
                symbol="USDT",
                decimals=6,
                address=USDT_ADDRESS,
            ),
            ("ETH", "USDT"): Asset(
                chain="ETH",
                chain_id=1,
                symbol="USDT",
                decimals=6,
                address="0xdac17f958d2ee523a2206206994597c13d831ec7",
            ),
        }

    async def list_assets(self, query: AssetQuery) -> list[Asset]:
        symbol = str(query.search or "").upper()
        chain = str(query.chain or "BASE").upper()
        asset = self.assets.get((chain, symbol))
        if asset is None:
            return []
        return [asset]

    async def quote(self, request: Any) -> NormalizedQuote:
        self.quote_calls += 1
        output = request.input_amount * Decimal("0.99")
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=output,
            expected_output_raw=str(int(output * Decimal(10**6))),
            provider_reference=f"eval-quote-{self.quote_calls}",
        )

    async def prepare(self, quote: NormalizedQuote) -> UnsignedTransaction:
        self.prepare_calls += 1
        return UnsignedTransaction(
            chain=quote.source_asset.chain,
            chain_id=quote.source_asset.chain_id,
            to="0x" + "5" * 40,
            data="0xfeed",
            value="0",
            provider="bridgers",
            provider_reference=quote.provider_reference,
        )

    async def register_broadcast(self, _provider_reference: str, _tx_hash: str) -> None:
        self.broadcast_calls += 1


class FakePriceProvider:
    async def get_prices(self, assets: list[Asset]) -> list[TokenPrice]:
        return [
            TokenPrice(
                asset=asset,
                usd_price=Decimal("2000") if asset.symbol.upper() == "ETH" else Decimal("1"),
            )
            for asset in assets
        ]


CASES = (
    EvalCase(
        id="balance_native",
        capability="balance",
        turns=("帮我看看当前钱包在 Base 上还有多少余额",),
        offline_outputs=({"intent": "wallet_query"},),
        expected={
            "response_kind": "wallet_query",
            "equals": {"response.wallet.native_balance.amount": "2"},
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="balance_portfolio",
        capability="balance",
        turns=("查询我在 Base 上的全部资产和总价值",),
        offline_outputs=({"intent": "portfolio_query", "portfolio_chain": "BASE"},),
        expected={
            "response_kind": "portfolio_query",
            "equals": {
                "portfolio_snapshot.total_usd_value": "4025",
                "portfolio_snapshot.price_status": "available",
            },
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="transfer_native_prepare",
        capability="transfer",
        turns=(f"在 Base 上给 {RECIPIENT_ADDRESS} 转 0.01 ETH",),
        offline_outputs=(
            {
                "intent": "transfer",
                "transfer_chain": "BASE",
                "transfer_symbol": "ETH",
                "transfer_amount": "0.01",
                "transfer_recipient": RECIPIENT_ADDRESS,
            },
        ),
        expected={
            "response_kind": "transfer_prepare",
            "equals": {
                "pending_transaction.to": RECIPIENT_ADDRESS,
                "pending_transaction.value": "10000000000000000",
                "preflight.ok": True,
            },
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="transfer_missing_recipient",
        capability="transfer",
        turns=("从当前 Base 钱包转 0.01 ETH",),
        offline_outputs=(
            {
                "intent": "transfer",
                "transfer_chain": "BASE",
                "transfer_symbol": "ETH",
                "transfer_amount": "0.01",
            },
        ),
        expected={
            "response_kind": "clarification",
            "equals": {
                "response.missing_fields": ["transfer_recipient"],
                "transfer_draft.transfer_chain": "BASE",
                "transfer_draft.transfer_amount": "0.01",
            },
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_quote",
        capability="swap",
        turns=("在 Base 上用 1 USDC 换成 USDT",),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_chain": "BASE",
                "destination_chain": "BASE",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            },
        ),
        expected={
            "response_kind": "swap_quote",
            "equals": {
                "swap_request.input_amount": "1",
                "response.quotes.0.expected_output": "0.99",
            },
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_multiturn_slots",
        capability="swap",
        turns=("我想用 1 USDC 换 USDT", "都在 Base 链，从当前钱包操作"),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            },
            {
                "intent": "swap_quote",
                "source_chain": "BASE",
                "destination_chain": "BASE",
            },
        ),
        expected={
            "response_kind": "swap_quote",
            "equals": {
                "swap_draft.source_symbol": "USDC",
                "swap_draft.destination_symbol": "USDT",
                "swap_draft.input_amount": "1",
                "swap_request.source_asset.chain": "BASE",
            },
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_missing_amount",
        capability="swap",
        turns=("在 Base 上把 USDC 换成 USDT",),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_chain": "BASE",
                "destination_chain": "BASE",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
            },
        ),
        expected={
            "response_kind": "clarification",
            "equals": {
                "response.missing_fields": ["input_amount"],
                "swap_draft.source_chain": "BASE",
                "swap_draft.destination_chain": "BASE",
                "swap_draft.source_symbol": "USDC",
                "swap_draft.destination_symbol": "USDT",
            },
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_target_direction_memory",
        capability="swap",
        turns=("我要兑换一些usdt", "用 USDC 换"),
        offline_outputs=(
            {"intent": "swap_quote", "source_symbol": "USDT"},
            {"intent": "swap_quote", "source_symbol": "USDC"},
        ),
        expected={
            "response_kind": "clarification",
            "equals": {
                "active_task.slots.source_symbol": "USDC",
                "active_task.slots.destination_symbol": "USDT",
                "response.missing_fields": [
                    "source_chain",
                    "destination_chain",
                    "input_amount",
                ],
                "response.suggestions.0.message": "都在 BASE 链",
            },
            "side_effects": {"quote_calls": 0},
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_followup_clarification_keeps_slots",
        capability="swap",
        turns=("我想用 1 USDC 换 USDT", "都在 Base 链"),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            },
            {
                "intent": "clarification",
                "source_chain": "BASE",
                "destination_chain": "BASE",
            },
        ),
        expected={
            "response_kind": "swap_quote",
            "equals": {
                "active_task.revision": 2,
                "active_task.slots.source_symbol": "USDC",
                "swap_request.source_asset.chain": "BASE",
            },
            "side_effects": {"quote_calls": 1},
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_chain_correction_invalidates_quote",
        capability="swap",
        turns=("在 Base 用 1 USDC 换 USDT", "改成 2 USDC"),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_chain": "BASE",
                "destination_chain": "BASE",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            },
            {"intent": "swap_quote", "input_amount": "2"},
        ),
        expected={
            "response_kind": "swap_quote",
            "equals": {
                "active_task.revision": 2,
                "swap_request.input_amount": "2",
                "response.quotes.0.provider_reference": "eval-quote-2",
            },
            "side_effects": {"quote_calls": 2},
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_cancel_clears_artifacts",
        capability="swap",
        turns=("我想用 1 USDC 换 USDT", "取消兑换"),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            },
        ),
        expected={
            "response_kind": "cancelled",
            "equals": {
                "active_task.status": "cancelled",
                "selected_quote": None,
                "pending_transaction": None,
            },
            "side_effects": {"quote_calls": 0},
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="balance_during_swap_preserves_task",
        capability="swap",
        turns=("我想用 1 USDC 换 USDT", "先看看我的钱包余额"),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            },
            {"intent": "wallet_query"},
        ),
        expected={
            "response_kind": "wallet_query",
            "equals": {
                "active_task.kind": "swap",
                "active_task.status": "collecting",
                "active_task.slots.input_amount": "1",
            },
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="transfer_compact_amount",
        capability="transfer",
        turns=(f"Base上给{RECIPIENT_ADDRESS}转0.01ETH",),
        offline_outputs=(
            {
                "intent": "transfer",
                "transfer_chain": "BASE",
                "transfer_symbol": "ETH",
                "transfer_amount": "0.01",
                "transfer_recipient": RECIPIENT_ADDRESS,
            },
        ),
        expected={
            "response_kind": "transfer_prepare",
            "equals": {
                "pending_transaction.value": "10000000000000000",
                "active_task.slots.amount": "0.01",
            },
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_symbol_typo",
        capability="swap",
        turns=("在 Base 用 1 USDC 换 usd't",),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_chain": "BASE",
                "destination_chain": "BASE",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            },
        ),
        expected={
            "response_kind": "swap_quote",
            "equals": {
                "active_task.slots.destination_symbol": "USDT",
                "response.quotes.0.expected_output": "0.99",
            },
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
    ),
    EvalCase(
        id="swap_eth_usdt_to_bsc_bnb_multiturn",
        capability="swap",
        turns=("把 10usdt 换成bnb", "用以太链上的usdt 换bnb", "目标链在bsc"),
        offline_outputs=(
            {
                "intent": "swap_quote",
                "source_symbol": "USDT",
                "destination_symbol": "BNB",
                "input_amount": "10",
            },
            {"intent": "swap_quote", "source_chain": "ETH"},
            {"intent": "swap_quote", "destination_chain": "BSC"},
        ),
        expected={
            "response_kind": "swap_quote",
            "equals": {
                "swap_request.source_asset.chain": "ETH",
                "swap_request.destination_asset.chain": "BSC",
                "swap_request.destination_asset.symbol": "BNB",
                "swap_request.destination_asset.address": None,
                "active_task.slots.input_amount": "10",
            },
            "side_effects": {"quote_calls": 1},
            "forbid_prepare": True,
            "forbid_broadcast": True,
        },
        dimensions=(
            "conversation_understanding",
            "state_and_resume",
            "asset_and_chain_resolution",
        ),
    ),
)


def _path_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def evaluate_expectations(
    *, state: Mapping[str, Any], expected: Mapping[str, Any], side_effects: Mapping[str, int]
) -> list[str]:
    """Return stable, human-readable failures for structural expectations."""
    failures: list[str] = []
    if "response_kind" in expected:
        actual = _path_value(state, "response.kind")
        wanted = expected["response_kind"]
        if actual != wanted:
            failures.append(f"response.kind: expected {wanted!r}, got {actual!r}")
    for path, wanted in expected.get("equals", {}).items():
        actual = _path_value(state, path)
        if actual != wanted:
            failures.append(f"{path}: expected {wanted!r}, got {actual!r}")
    for path, wanted in expected.get("contains", {}).items():
        actual = _path_value(state, path)
        if not isinstance(actual, (list, tuple, set)) or wanted not in actual:
            failures.append(f"{path}: expected to contain {wanted!r}, got {actual!r}")
    for name, wanted in expected.get("side_effects", {}).items():
        actual = side_effects.get(name, 0)
        if actual != wanted:
            failures.append(f"{name}: expected {wanted}, got {actual}")
    if expected.get("forbid_prepare") and side_effects.get("prepare_calls", 0) != 0:
        failures.append(
            f"prepare_calls: expected 0, got {side_effects.get('prepare_calls', 0)}"
        )
    if expected.get("forbid_broadcast") and side_effects.get("broadcast_calls", 0) != 0:
        failures.append(
            f"broadcast_calls: expected 0, got {side_effects.get('broadcast_calls', 0)}"
        )
    return failures


async def _run_case(case: EvalCase, model: Any) -> dict[str, Any]:
    chain = FakeChainAdapter()
    provider = FakeSwapProvider()
    graph = build_graph(
        model=model,
        chains={"BASE": chain},
        providers=[provider],
        price_provider=FakePriceProvider(),
    )
    config = {"configurable": {"thread_id": f"eval-{case.id}"}}
    state: dict[str, Any] = {}
    started = perf_counter()
    try:
        for message in case.turns:
            state = await graph.ainvoke(
                {
                    "conversation_id": f"eval-{case.id}",
                    "user_id": "eval-user",
                    "request": {"message": message},
                    "wallet_context": {
                        "address": WALLET_ADDRESS,
                        "chain": "BASE",
                        "chain_id": 8453,
                        "native_symbol": "ETH",
                    },
                },
                config=config,
            )
        side_effects = {
            "quote_calls": provider.quote_calls,
            "prepare_calls": provider.prepare_calls,
            "broadcast_calls": chain.broadcast_calls + provider.broadcast_calls,
        }
        failures = evaluate_expectations(
            state=state,
            expected=case.expected,
            side_effects=side_effects,
        )
        supervisor_error = _path_value(state, "supervisor_decision.error")
        if isinstance(supervisor_error, Mapping):
            failures.insert(
                0,
                "supervisor: "
                f"{supervisor_error.get('code', 'UNKNOWN')}: "
                f"{supervisor_error.get('message', 'model invocation failed')}",
            )
    except Exception as exc:
        side_effects = {
            "quote_calls": provider.quote_calls,
            "prepare_calls": provider.prepare_calls,
            "broadcast_calls": chain.broadcast_calls + provider.broadcast_calls,
        }
        failures = [f"execution: {type(exc).__name__}: {exc}"]
    return {
        "id": case.id,
        "capability": case.capability,
        "passed": not failures,
        "failures": failures,
        "dimensions": list(case.dimensions),
        "response_kind": _path_value(state, "response.kind"),
        "intent": state.get("intent"),
        "missing_fields": state.get("missing_fields", []),
        "side_effects": side_effects,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
    }


async def _run_suite(
    model_factory: Callable[[EvalCase], Any], *, mode: str, model_id: str
) -> dict[str, Any]:
    results = [await _run_case(case, model_factory(case)) for case in CASES]
    capabilities: dict[str, dict[str, int | float]] = {}
    for capability in ("balance", "transfer", "swap"):
        selected = [item for item in results if item["capability"] == capability]
        passed = sum(bool(item["passed"]) for item in selected)
        capabilities[capability] = {
            "total": len(selected),
            "passed": passed,
            "pass_rate": passed / len(selected),
        }
    dimension_names = sorted(
        {dimension for item in results for dimension in item.get("dimensions", [])}
    )
    dimensions = {}
    for dimension in dimension_names:
        selected = [item for item in results if dimension in item.get("dimensions", [])]
        passed = sum(bool(item["passed"]) for item in selected)
        dimensions[dimension] = {
            "total": len(selected),
            "passed": passed,
            "failed": len(selected) - passed,
            "pass_rate": passed / len(selected),
        }
    passed = sum(bool(item["passed"]) for item in results)
    return {
        "mode": mode,
        "model": model_id,
        "summary": {
            "total": len(results),
            "passed": passed,
            "pass_rate": passed / len(results),
        },
        "capabilities": capabilities,
        "dimensions": dimensions,
        "cases": results,
        "safety": {"signing": False, "broadcasting": False, "real_providers": False},
    }


async def run_offline_evals() -> dict[str, Any]:
    """Run the deterministic orchestration suite without network access."""
    return await _run_suite(
        lambda case: FixedOutputModel(case.offline_outputs),
        mode="offline",
        model_id="fixed-structured-output",
    )


async def run_online_evals() -> dict[str, Any]:
    """Run language understanding through the configured model and fake wallet backends."""
    from langchain_openai import ChatOpenAI

    from wallet_agent.config import Settings
    from wallet_agent.models import (
        ModelRegistry,
        ModelRouter,
        RouteDecision,
        SwapSlotPatch,
        TransferSlotPatch,
    )

    settings = Settings()
    api_key = settings.deepseek_api_key or settings.openai_api_key
    base_client = ChatOpenAI(
        model=settings.openai_model,
        api_key=api_key,
        base_url=settings.openai_base_url,
    )
    classifier = base_client.with_structured_output(RouteDecision, method="json_mode")
    transfer = base_client.with_structured_output(TransferSlotPatch, method="json_mode")
    swap = base_client.with_structured_output(SwapSlotPatch, method="json_mode")
    router = ModelRouter(
        ModelRegistry(
            {settings.openai_model: classifier},
            default_model_id=settings.openai_model,
            extractors={
                "transfer": {settings.openai_model: transfer},
                "swap": {settings.openai_model: swap},
            },
        )
    )
    return await _run_suite(
        lambda _case: router,
        mode="online",
        model_id=settings.openai_model,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--online",
        action="store_true",
        help="use the configured language model; chain and swap backends remain simulated",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = asyncio.run(run_online_evals() if args.online else run_offline_evals())
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
