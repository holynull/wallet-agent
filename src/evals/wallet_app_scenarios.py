"""Offline, public-contract swap scenarios for the wallet application.

The harness deliberately wires the real compiled graph and FastAPI app.  Its
chain/provider/model implementations are deterministic boundary fakes: they
record only sanitized evidence and never hold signing material or call a real
transport.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from wallet_agent.api.app import create_app
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.domain.chains import Capability, CapabilitySnapshot
from wallet_agent.domain.models import (
    Asset,
    AssetQuery,
    DepositOrder,
    FeeEstimate,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    TokenBalance,
    TransactionRecord,
    TransactionStatus,
    UnsignedTransaction,
)
from wallet_agent.graph import build_graph
from wallet_agent.persistence import InMemorySessionStore

from .wallet_app_simulator import (
    Eip1193WalletSimulator,
    EvidenceLedger,
    FaultOutcome,
    FaultPlan,
    FaultSequence,
    InvariantResult,
    LifecycleReport,
    LifecycleStep,
    WalletAppClient,
    sanitize_evidence,
)

WALLET_ADDRESS = "0x" + "1" * 40
RECIPIENT_ADDRESS = "0x" + "2" * 40
USDC_ADDRESS = "0x" + "3" * 40
USDT_ADDRESS = "0x" + "4" * 40
SWAP_DEPOSIT_ADDRESS = "0x" + "5" * 40


@dataclass(frozen=True)
class ScenarioDefinition:
    id: str
    provider: str
    allowance_required: bool
    fault_plan: FaultPlan
    wallet_hashes: tuple[str, ...]
    expected_final_stage: str
    expected_wallet_sends: int
    expected_register_attempts: int
    restart_after_approval: bool = False
    dimensions: tuple[str, ...] = ("wallet_api_contract", "safety")


SCENARIOS = {
    "erc20_swap_without_approval": ScenarioDefinition(
        id="erc20_swap_without_approval",
        provider="bridgers",
        allowance_required=False,
        fault_plan=FaultPlan(
            allowances=(FaultOutcome("sufficient", "10000000"),),
            provider_register=(FaultOutcome("success", "order-no-approval"),),
            provider_status=(FaultOutcome("completed"),),
        ),
        wallet_hashes=("0x" + "a" * 64,),
        expected_final_stage="completed",
        expected_wallet_sends=1,
        expected_register_attempts=1,
        dimensions=("wallet_api_contract", "broadcast_and_confirmation", "safety"),
    ),
    "erc20_swap_with_approval": ScenarioDefinition(
        id="erc20_swap_with_approval",
        provider="bridgers",
        allowance_required=True,
        fault_plan=FaultPlan(
            allowances=(FaultOutcome("insufficient", "0"), FaultOutcome("sufficient", "10000000")),
            chain_receipts=(FaultOutcome("confirmed", {"status": "0x1"}),),
            provider_register=(FaultOutcome("success", "order-with-approval"),),
            provider_status=(FaultOutcome("completed"),),
        ),
        wallet_hashes=("0x" + "c" * 64, "0x" + "b" * 64),
        expected_final_stage="completed",
        expected_wallet_sends=2,
        expected_register_attempts=1,
        dimensions=("wallet_api_contract", "broadcast_and_confirmation", "safety"),
    ),
}


def literal_swap_request() -> dict[str, Any]:
    return {
        "source_asset": {
            "chain": "ETH",
            "chain_id": 1,
            "symbol": "USDC",
            "decimals": 6,
            "address": USDC_ADDRESS,
        },
        "destination_asset": {
            "chain": "ETH",
            "chain_id": 1,
            "symbol": "USDT",
            "decimals": 6,
            "address": USDT_ADDRESS,
        },
        "input_amount": "10",
        "input_amount_raw": "10000000",
        "sender_address": WALLET_ADDRESS,
        "recipient_address": RECIPIENT_ADDRESS,
        "refund_address": WALLET_ADDRESS,
        "slippage_bps": 100,
    }


class FixedSwapModel:
    """A tiny turn-aware model with no network or provider dependency."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, _request: Any) -> dict[str, str]:
        self.calls += 1
        return {"intent": "swap_quote" if self.calls == 1 else "swap_status"}


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


def _sequence(outcomes: Sequence[FaultOutcome], *, fallback: FaultOutcome) -> FaultSequence:
    return FaultSequence(outcomes=tuple(outcomes), fallback=fallback)


class RecordingChainAdapter:
    chain = "ETH"
    chain_id = 1

    def __init__(
        self, definition: ScenarioDefinition, ledger: EvidenceLedger | None = None
    ) -> None:
        self.definition = definition
        self.ledger = ledger or EvidenceLedger()
        plan = definition.fault_plan
        self._allowances = _sequence(
            plan.allowances,
            fallback=FaultOutcome("sufficient", "10000000"),
        )
        self._receipts = _sequence(
            plan.chain_receipts,
            fallback=FaultOutcome("not_found"),
        )
        self._transactions = _sequence(
            plan.chain_transactions,
            fallback=FaultOutcome("not_found"),
        )
        self._statuses = _sequence(
            plan.chain_statuses,
            fallback=FaultOutcome("confirmed"),
        )
        self.native_asset = Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18)
        self.capabilities = CapabilitySnapshot(
            chain="ETH",
            available=frozenset(
                {
                    Capability.ADDRESS_VALIDATION,
                    Capability.BALANCES,
                    Capability.FEES,
                    Capability.TRANSACTION_STATUS,
                }
            ),
        )

    def _record(self, operation: str, outcome: FaultOutcome, **arguments: Any) -> None:
        self.ledger.record(
            "chain",
            operation,
            **_dump(arguments),
            arguments=_dump(arguments),
            outcome={
                "kind": outcome.kind,
                "value": _dump(outcome.value),
                "message": outcome.message,
            },
        )

    @staticmethod
    def _raise(outcome: FaultOutcome) -> None:
        if outcome.kind == "rpc_error":
            raise RuntimeError(outcome.message or "chain RPC error")
        if outcome.kind == "raise":
            raise RuntimeError(outcome.message or "chain fault")

    async def validate_address(self, address: str) -> bool:
        return isinstance(address, str) and address.startswith("0x") and len(address) == 42

    async def get_native_balance(self, address: str) -> TokenBalance:
        return TokenBalance(
            asset=self.native_asset,
            amount=Decimal("1"),
            amount_raw="1000000000000000000",
        )

    async def get_token_balances(self, address: str) -> list[TokenBalance]:
        del address
        token = Asset(chain="ETH", chain_id=1, symbol="USDC", decimals=6, address=USDC_ADDRESS)
        return [TokenBalance(asset=token, amount=Decimal("100"), amount_raw="100000000")]

    async def get_token_balance(self, asset: Asset, owner: str) -> TokenBalance:
        del owner
        return TokenBalance(asset=asset, amount=Decimal("100"), amount_raw="100000000")

    async def get_allowance(self, token: Asset, owner: str, spender: str) -> str:
        outcome = self._allowances.next()
        self._record("get_allowance", outcome, token=token, owner=owner, spender=spender)
        if outcome.kind == "read_error":
            raise RuntimeError(outcome.message or "allowance read failed")
        if outcome.kind in {"insufficient", "sufficient"}:
            return str(outcome.value or "0")
        self._raise(outcome)
        return "0"

    def build_erc20_approve(
        self, *, token: Asset, owner: str, spender: str, amount_raw: str
    ) -> UnsignedTransaction:
        outcome = FaultOutcome("success", "approval")
        data = (
            "0x095ea7b3"
            + spender.removeprefix("0x").lower().rjust(64, "0")
            + format(int(amount_raw), "064x")
        )
        tx = UnsignedTransaction(
            chain="ETH", chain_id=1, to=str(token.address), data=data, value="0"
        )
        self._record(
            "build_erc20_approve",
            outcome,
            token=token,
            owner=owner,
            spender=spender,
            amount_raw=amount_raw,
            transaction=tx,
        )
        return tx

    async def estimate_fee(self, *, to: str | None = None, data: str | None = None) -> FeeEstimate:
        outcome = FaultOutcome("success", "21000")
        fee_asset = self.native_asset
        fee = FeeEstimate(
            chain="ETH",
            chain_id=1,
            asset=fee_asset,
            amount=Decimal("0.00042"),
            amount_raw="420000000000000",
            gas_limit="21000",
            max_fee_per_gas="20000000000",
            max_priority_fee_per_gas="1000000000",
        )
        self._record("estimate_fee", outcome, to=to, data=data, fee=fee)
        return fee

    async def get_transaction_receipt(self, tx_hash: str) -> dict[str, object] | None:
        outcome = self._receipts.next()
        self._record("get_transaction_receipt", outcome, tx_hash=tx_hash)
        if outcome.kind in {"not_found", "pending"}:
            return None
        if outcome.kind in {"confirmed", "reverted"}:
            return dict(outcome.value or {})
        self._raise(outcome)
        return None

    async def get_transaction(self, tx_hash: str) -> dict[str, object] | None:
        outcome = self._transactions.next()
        self._record("get_transaction", outcome, tx_hash=tx_hash)
        if outcome.kind == "not_found":
            return None
        if outcome.kind == "visible":
            return dict(outcome.value) if isinstance(outcome.value, Mapping) else {"hash": tx_hash}
        self._raise(outcome)
        return None

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus:
        outcome = self._statuses.next()
        self._record("get_transaction_status", outcome, tx_hash=tx_hash)
        values = {
            "pending": TransactionStatus.PENDING,
            "confirmed": TransactionStatus.CONFIRMED,
            "failed": TransactionStatus.FAILED,
            "dropped": TransactionStatus.DROPPED,
        }
        if outcome.kind in values:
            return values[outcome.kind]
        self._raise(outcome)
        return TransactionStatus.UNKNOWN

    async def get_transaction_history(
        self, address: str, *, limit: int = 20
    ) -> list[TransactionRecord]:
        del address, limit
        return []

    def build_native_transfer(
        self, *, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction:
        del from_address
        return UnsignedTransaction(
            chain="ETH", chain_id=1, to=to_address, data="0x", value=amount_raw
        )

    def build_erc20_transfer(
        self,
        *,
        token: Asset,
        from_address: str,
        to_address: str,
        amount_raw: str,
    ) -> UnsignedTransaction:
        del from_address, to_address, amount_raw
        return UnsignedTransaction(
            chain="ETH", chain_id=1, to=str(token.address), data="0xa9059cbb", value="0"
        )


class RecordingProvider:
    provider_name: str

    def __init__(
        self,
        definition: ScenarioDefinition,
        *,
        provider_name: str,
        alternate: bool = False,
        ledger: EvidenceLedger | None = None,
    ) -> None:
        self.definition = definition
        self.provider_name = provider_name
        self.alternate = alternate
        self.ledger = ledger or EvidenceLedger()
        plan = definition.fault_plan
        self._prepare = _sequence(plan.provider_prepare, fallback=FaultOutcome("success"))
        self._register = _sequence(
            plan.provider_register, fallback=FaultOutcome("success", "order-fallback")
        )
        self._status = _sequence(plan.provider_status, fallback=FaultOutcome("completed"))
        self._quote_count = 0

    def _record(self, operation: str, outcome: FaultOutcome, **arguments: Any) -> None:
        self.ledger.record(
            "provider",
            operation,
            provider=self.provider_name,
            **_dump(arguments),
            arguments=_dump(arguments),
            outcome={
                "kind": outcome.kind,
                "value": _dump(outcome.value),
                "message": outcome.message,
            },
        )

    async def list_assets(self, query: AssetQuery) -> list[Asset]:
        outcome = FaultOutcome("success")
        self._record("list_assets", outcome, query=query)
        source = Asset(chain="ETH", chain_id=1, symbol="USDC", decimals=6, address=USDC_ADDRESS)
        destination = Asset(
            chain="ETH", chain_id=1, symbol="USDT", decimals=6, address=USDT_ADDRESS
        )
        text = str(query.search or "").upper()
        if text and text not in {"USDC", "USDT"}:
            return []
        return [source, destination]

    async def quote(self, request: SwapQuoteRequest) -> NormalizedQuote:
        self._quote_count += 1
        outcome = FaultOutcome("success", f"quote-{self.provider_name}")
        self._record("quote", outcome, request=request)
        requirement = None
        if self.definition.allowance_required:
            from wallet_agent.domain.models import AllowanceRequirement

            requirement = AllowanceRequirement(
                token=request.source_asset,
                owner=request.sender_address,
                spender=SWAP_DEPOSIT_ADDRESS,
                required_amount_raw=request.input_amount_raw,
                current_allowance_raw="0",
            )
        return NormalizedQuote(
            provider=self.provider_name,
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("9.9"),
            expected_output_raw="9900000",
            provider_reference=(
                f"{self.provider_name}-alternate-quote"
                if self.alternate
                else f"{self.provider_name}-primary-quote"
            ),
            allowance_requirement=requirement,
        )

    async def prepare(self, quote: NormalizedQuote) -> UnsignedTransaction | DepositOrder:
        outcome = self._prepare.next()
        self._record("prepare", outcome, provider_reference=quote.provider_reference, quote=quote)
        if outcome.kind == "success":
            return UnsignedTransaction(
                chain="ETH",
                chain_id=1,
                to=SWAP_DEPOSIT_ADDRESS,
                data="0xfeedface",
                value="0",
                provider=self.provider_name,
                provider_reference=quote.provider_reference,
            )
        if outcome.kind == "malformed":
            return outcome.value  # type: ignore[return-value]
        if outcome.kind == "error":
            raise RuntimeError(outcome.message or "provider prepare failed")
        raise RuntimeError(outcome.message or outcome.kind)

    async def register_broadcast(self, provider_reference: str, tx_hash: str) -> ProviderOrder:
        outcome = self._register.next()
        self._record(
            "register_broadcast",
            outcome,
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )
        if outcome.kind == "success":
            return ProviderOrder(
                provider=self.provider_name,
                provider_order_id=str(outcome.value or "order"),
                provider_reference=provider_reference,
                tx_hash=tx_hash,
            )
        if outcome.kind == "timeout":
            raise TimeoutError(outcome.message or "provider register timed out")
        if outcome.kind == "error":
            raise RuntimeError(outcome.message or "provider register failed")
        raise RuntimeError(outcome.message or outcome.kind)

    async def get_status(self, order: ProviderOrder) -> NormalizedOrderStatus:
        outcome = self._status.next()
        self._record("get_status", outcome, order=order)
        if outcome.kind in {"processing", "completed", "failed", "refunded", "timed_out"}:
            return NormalizedOrderStatus(
                provider=self.provider_name,
                provider_order_id=order.provider_order_id,
                provider_reference=order.provider_reference,
                status=outcome.kind,
                tx_hash=order.tx_hash,
            )
        if outcome.kind == "malformed":
            return outcome.value  # type: ignore[return-value]
        if outcome.kind == "timeout":
            raise TimeoutError(outcome.message or "provider status timed out")
        if outcome.kind == "error":
            raise RuntimeError(outcome.message or "provider status failed")
        raise RuntimeError(outcome.message or outcome.kind)


@dataclass
class ScenarioRuntime:
    definition: ScenarioDefinition
    http: httpx.AsyncClient
    client: WalletAppClient
    wallet: Eip1193WalletSimulator
    chain: RecordingChainAdapter
    primary: RecordingProvider
    alternate: RecordingProvider
    ledger: EvidenceLedger
    construction_manifest: dict[str, Any] = field(default_factory=dict)


def _step(client: WalletAppClient, operation: str, evidence: Mapping[str, Any]) -> None:
    client.steps.append(
        LifecycleStep(
            operation=operation,
            stage_before=client.stage,
            stage_after=client.stage,
            http_status=None,
            error_code=None,
            evidence=sanitize_evidence(dict(evidence)),
        )
    )


def _safe_evidence(value: Any) -> bool:
    forbidden = {
        "authorization",
        "apikey",
        "clientsecret",
        "credential",
        "mnemonic",
        "password",
        "privatekey",
        "rawtransaction",
        "seed",
        "seedphrase",
        "signature",
        "signedrawtransaction",
        "signer",
        "walletclient",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            canonical = "".join(character for character in str(key).lower() if character.isalnum())
            if canonical in forbidden and item != "[REDACTED]":
                return False
            if not _safe_evidence(item):
                return False
    elif isinstance(value, (list, tuple)):
        return all(_safe_evidence(item) for item in value)
    elif isinstance(value, str) and value != "[REDACTED]":
        canonical = "".join(character for character in value.lower() if character.isalnum())
        return canonical not in forbidden
    return True


def _public_request_recorder(ledger: EvidenceLedger):
    async def record(request: httpx.Request) -> None:
        try:
            body: Any = json.loads(request.content) if request.content else None
        except (TypeError, ValueError, UnicodeDecodeError):
            body = request.content.decode("utf-8", errors="replace")
        headers = dict(request.headers)
        raw_evidence = {
            "headers": headers,
            "query": dict(request.url.params),
            "body": body,
        }
        ledger.record(
            "public_http",
            "request",
            method=request.method,
            path=request.url.path,
            **raw_evidence,
            private_material_detected=not _safe_evidence(raw_evidence),
        )

    return record


def _is_wallet_action(value: Any) -> bool:
    canonical = "".join(character for character in str(value).lower() if character.isalnum())
    return canonical.startswith("wallet") or canonical in {
        "ethrequestaccounts",
        "ethsendtransaction",
        "ethsign",
        "ethsigntransaction",
        "ethsigntypeddata",
        "ethsigntypeddatav1",
        "ethsigntypeddatav3",
        "ethsigntypeddatav4",
        "personalsign",
    }


def _contains_wallet_action(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                "".join(character for character in str(key).lower() if character.isalnum())
                in {"method", "operation"}
                and _is_wallet_action(item)
            )
            or _contains_wallet_action(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_wallet_action(item) for item in value)
    return False


def _selected_reference(step: LifecycleStep) -> str | None:
    selected = step.evidence.get("selected_quote")
    if isinstance(selected, Mapping) and selected.get("provider_reference"):
        return str(selected["provider_reference"])
    reference = step.evidence.get("selected_provider_reference")
    return str(reference) if reference else None


def _lifecycle_invariants(
    *,
    events: Sequence[Mapping[str, Any]],
    steps: Sequence[LifecycleStep],
    construction_manifest: Mapping[str, Any],
    session: Mapping[str, Any],
    max_attempts: int,
) -> tuple[InvariantResult, ...]:
    public_requests = [
        event
        for event in events
        if event.get("actor") == "public_http" and event.get("operation") == "request"
    ]
    signing_material_safe = (
        all(_safe_evidence(event) for event in events)
        and all(_safe_evidence(step.evidence) for step in steps)
        and not any(event.get("private_material_detected") is True for event in public_requests)
    )
    server_wallet_actions_safe = (
        construction_manifest.get("wallet_injected_into_graph") is False
        and construction_manifest.get("wallet_injected_into_app") is False
        and all(
            event.get("actor") == "wallet" or not _contains_wallet_action(event) for event in events
        )
    )

    register_events = [
        event
        for event in events
        if event.get("actor") == "provider" and event.get("operation") == "register_broadcast"
    ]
    wallet_results = [
        event
        for event in events
        if event.get("actor") == "wallet"
        and event.get("operation") == "eth_sendTransaction_result"
        and event.get("success") is True
    ]
    broadcast_requests = [
        event for event in public_requests if str(event.get("path", "")).endswith("/broadcast")
    ]

    def registration_has_public_wallet_hash(registration: Mapping[str, Any]) -> bool:
        tx_hash = registration.get("tx_hash")
        register_sequence = int(registration.get("sequence", 0))
        return any(
            result.get("result") == tx_hash
            and int(result.get("sequence", 0)) < int(submission.get("sequence", 0))
            and int(submission.get("sequence", 0)) < register_sequence
            and isinstance(submission.get("body"), Mapping)
            and submission["body"].get("tx_hash") == tx_hash
            for result in wallet_results
            for submission in broadcast_requests
        )

    registration_safe = all(
        registration_has_public_wallet_hash(registration) for registration in register_events
    )

    successful_references = {
        reference
        for step in steps
        if step.operation == "select_quote"
        and step.http_status is not None
        and 200 <= step.http_status < 300
        and (reference := _selected_reference(step)) is not None
    }
    selection_requests = [
        event
        for event in public_requests
        if str(event.get("path", "")).endswith("/select-quote")
        and isinstance(event.get("body"), Mapping)
        and event["body"].get("provider_reference") in successful_references
    ]
    successful_selections = [
        event
        for event in events
        if event.get("actor") == "public_http"
        and event.get("operation") == "select_quote_result"
        and event.get("success") is True
        and event.get("provider_reference") in successful_references
    ]
    if not successful_selections:
        successful_selections = selection_requests
    prepare_events = [
        event
        for event in events
        if event.get("actor") == "provider" and event.get("operation") == "prepare"
    ]
    quote_selection_safe = (
        len(session.get("quote_candidates", [])) > 1
        and bool(successful_references)
        and bool(prepare_events)
        and all(
            (
                prior_selections := [
                    selection
                    for selection in successful_selections
                    if int(selection.get("sequence", 0)) < int(prepare.get("sequence", 0))
                ]
            )
            and (
                prior_selections[-1].get("provider_reference")
                or prior_selections[-1].get("body", {}).get("provider_reference")
            )
            == prepare.get("provider_reference")
            for prepare in prepare_events
        )
    )
    return (
        InvariantResult(
            "no_signing_material_to_server",
            signing_material_safe,
            {"public_requests": public_requests},
        ),
        InvariantResult("no_server_wallet_actions", server_wallet_actions_safe),
        InvariantResult("no_register_before_wallet_hash", registration_safe),
        InvariantResult("explicit_quote_selection", quote_selection_safe),
        InvariantResult(
            "bounded_progress",
            len([step for step in steps if step.operation == "continue"]) <= max_attempts,
        ),
    )


async def build_scenario_runtime(definition: ScenarioDefinition) -> ScenarioRuntime:
    ledger = EvidenceLedger()
    chain = RecordingChainAdapter(definition, ledger)
    primary = RecordingProvider(definition, provider_name=definition.provider, ledger=ledger)
    alternate_name = "omnibridge" if definition.provider == "bridgers" else "bridgers"
    alternate = RecordingProvider(
        definition, provider_name=alternate_name, alternate=True, ledger=ledger
    )
    providers = {primary.provider_name: primary, alternate.provider_name: alternate}
    graph = build_graph(
        model=FixedSwapModel(),
        providers=providers,
        chains={"ETH": chain},
        max_poll_attempts=1,
    )
    app = create_app(
        graph=graph,
        providers=providers,
        chain_registry=ChainAdapterRegistry({"ETH": chain}),
        store=InMemorySessionStore(),
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://wallet.test",
        event_hooks={"request": [_public_request_recorder(ledger)]},
    )
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=(WALLET_ADDRESS,),
        ledger=ledger,
        send_outcomes=FaultSequence(
            outcomes=tuple(FaultOutcome("success", value) for value in definition.wallet_hashes),
            fallback=FaultOutcome("rpc_error", message="unexpected wallet send"),
        ),
    )
    client = WalletAppClient(
        http,
        user_id="eval-user",
        address=WALLET_ADDRESS,
        chain="ETH",
        turn_metadata={"swap_request": literal_swap_request()},
    )
    return ScenarioRuntime(
        definition,
        http,
        client,
        wallet,
        chain,
        primary,
        alternate,
        ledger,
        construction_manifest={
            "wallet_injected_into_graph": False,
            "wallet_injected_into_app": False,
            "settings_loaded": False,
            "real_provider_instantiated": False,
            "real_transport_instantiated": False,
        },
    )


def wallet_transaction(unsigned: Mapping[str, Any], from_address: str) -> dict[str, str]:
    transaction = {
        "from": from_address,
        "to": str(unsigned["to"]),
        "data": str(unsigned.get("data") or "0x"),
        "value": hex(int(str(unsigned.get("value") or "0"), 0)),
    }
    for source, target in (
        ("gas_limit", "gas"),
        ("max_fee_per_gas", "maxFeePerGas"),
        ("max_priority_fee_per_gas", "maxPriorityFeePerGas"),
    ):
        if unsigned.get(source) is not None:
            transaction[target] = hex(int(str(unsigned[source]), 0))
    return transaction


async def drive_scenario(runtime: ScenarioRuntime, max_attempts: int = 3) -> LifecycleReport:
    definition = runtime.definition
    client = runtime.client
    failures: list[str] = []
    try:
        await client.turn("Swap 10 USDC to USDT on Ethereum")
        session = await client.session()
        candidates = session.get("quote_candidates") or []
        primary_reference = next(
            item["provider_reference"]
            for item in candidates
            if item.get("provider") == definition.provider
        )
        await client.select_quote(primary_reference)
        runtime.ledger.record(
            "public_http",
            "select_quote_result",
            provider_reference=primary_reference,
            success=True,
        )
        confirmed = await client.confirm(True)
        if definition.allowance_required:
            approval = confirmed.get("approval_transaction")
            if not approval:
                raise AssertionError("approval transaction was not projected after confirmation")
            approval_tx = wallet_transaction(approval, WALLET_ADDRESS)
            approval_hash = await runtime.wallet.request("eth_sendTransaction", [approval_tx])
            runtime.ledger.record(
                "wallet",
                "eth_sendTransaction_result",
                result=approval_hash,
                success=True,
            )
            _step(
                client,
                "wallet_approval",
                {"method": "eth_sendTransaction", "transaction": approval_tx},
            )
            await client.submit_approval_hash("ETH", str(approval_hash))
            prepared: dict[str, Any] | None = None
            for _attempt in range(max_attempts):
                continued = await client.continue_swap()
                prepared = continued.get("pending_transaction")
                if prepared:
                    break
            if not prepared:
                raise AssertionError("approval continuation did not produce a swap transaction")
        else:
            prepared = confirmed.get("pending_transaction")
            if not prepared:
                raise AssertionError("confirmation did not produce a swap transaction")
        swap_tx = wallet_transaction(prepared, WALLET_ADDRESS)
        swap_hash = await runtime.wallet.request("eth_sendTransaction", [swap_tx])
        runtime.ledger.record(
            "wallet",
            "eth_sendTransaction_result",
            result=swap_hash,
            success=True,
        )
        _step(client, "wallet_swap", {"method": "eth_sendTransaction", "transaction": swap_tx})
        await client.submit_swap_hash("ETH", str(swap_hash))
        status_turn_index = len(client.steps)
        await client.turn("Check the swap status")
        if client.steps[status_turn_index].operation == "turn":
            status_turn = client.steps[status_turn_index]
            client.steps[status_turn_index] = LifecycleStep(
                operation="status_turn",
                stage_before=status_turn.stage_before,
                stage_after=status_turn.stage_after,
                http_status=status_turn.http_status,
                error_code=status_turn.error_code,
                evidence=status_turn.evidence,
            )
        final_session = await client.session()
        final_stage = str(final_session.get("stage") or client.stage or "")
    except Exception as exc:  # reports preserve evidence while keeping test assertions simple
        failures.append(str(exc))
        final_stage = str(client.stage or "failed")

    wallet_calls = tuple(
        event
        for event in runtime.ledger.events
        if event.get("actor") == "wallet" and event.get("operation") != "eth_sendTransaction_result"
    )
    provider_calls = tuple(
        event for event in runtime.ledger.events if event.get("actor") == "provider"
    )
    register_events = [
        event for event in provider_calls if event.get("operation") == "register_broadcast"
    ]
    wallet_hash_events = [
        event for event in wallet_calls if event.get("operation") == "eth_sendTransaction"
    ]
    invariants = _lifecycle_invariants(
        events=runtime.ledger.events,
        steps=client.steps,
        construction_manifest=runtime.construction_manifest,
        session=session if "session" in locals() else {},
        max_attempts=max_attempts,
    )
    if final_stage != definition.expected_final_stage:
        failures.append(
            f"expected final stage {definition.expected_final_stage}, got {final_stage}"
        )
    if len(wallet_hash_events) != definition.expected_wallet_sends:
        failures.append(
            f"expected {definition.expected_wallet_sends} wallet sends, "
            f"got {len(wallet_hash_events)}"
        )
    if len(register_events) != definition.expected_register_attempts:
        failures.append(
            f"expected {definition.expected_register_attempts} provider registrations, "
            f"got {len(register_events)}"
        )
    status = "passed" if not failures and all(item.passed for item in invariants) else "failed"
    return LifecycleReport(
        id=definition.id,
        status=status,
        final_stage=final_stage,
        steps=tuple(client.steps),
        wallet_calls=wallet_calls,
        provider_calls=provider_calls,
        invariants=invariants,
        failures=tuple(failures),
        dimensions=definition.dimensions,
    )


async def run_scenario(scenario_id: str, max_attempts: int = 3) -> LifecycleReport:
    try:
        definition = SCENARIOS[scenario_id]
    except KeyError as exc:
        raise ValueError(f"unknown scenario: {scenario_id}") from exc
    runtime = await build_scenario_runtime(definition)
    try:
        return await drive_scenario(runtime, max_attempts=max_attempts)
    finally:
        await runtime.http.aclose()
