# Wallet App Agent Evaluation Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully offline Wallet App lifecycle evaluator that drives the production FastAPI/SSE and compiled LangGraph interfaces from quote selection through approval, swap broadcast, recovery, and terminal status while proving non-custodial safety invariants.

**Architecture:** Add a reusable stateful HTTP/SSE client and programmable EIP-1193 simulator under `src/evals`, then construct deterministic provider and chain fakes around the real `create_app` and compiled graph. Scenario tests and the CLI use only public Wallet App endpoints after application construction; fault plans are immutable inputs, side effects are recorded at existing provider/chain boundaries, and reports contain sanitized public evidence rather than graph/store internals.

**Tech Stack:** Python 3.11, FastAPI, HTTPX `ASGITransport`, LangGraph 0.6.11, LangChain Core 0.3.86, Pydantic v2, pytest/pytest-asyncio, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-20-wallet-app-evaluation-phase-2-design.md`

## Global Constraints

- Python 3.11; retain LangGraph 0.6.11 and LangChain Core 0.3.86 from `uv.lock`.
- The default evaluator is completely offline: do not read `.env`, instantiate configured production providers/RPC transports, sign, or broadcast.
- No private key, mnemonic, seed phrase, signer, wallet client, signed raw transaction, authorization header, API key, or provider credential may enter a request, fixture, log, exception, or report.
- Use the existing compiled LangGraph, `create_app`, public REST/SSE handlers, `InMemorySessionStore`, and injectable provider/chain boundaries as the system under test.
- Lifecycle tests may construct the application and inspect deterministic fake evidence, but after construction they must not call graph nodes, graph methods, `app.state`, or the session store directly.
- App restart means discarding and reconstructing `WalletAppClient` while retaining the same FastAPI application, `ASGITransport`, graph checkpointer, and session store.
- Every continuation/status loop has an explicit attempt limit and reports bounded pending/timeout rather than looping forever or fabricating success.
- All eleven safety invariants are deterministic and must pass at 100%; a matching final stage cannot hide a failed invariant.
- Anvil, real read-only Provider/RPC smoke tests, backend-process restart, durable SQLite recovery, and browser rendering remain out of scope.
- Every behavior change follows RED → observed failure → minimal GREEN implementation → focused verification → commit.

## File Structure

- `src/evals/wallet_app_simulator.py`: SSE parser, structured HTTP errors, evidence redaction, fault sequences, report value objects, stateful public API client, and EIP-1193 wallet simulator.
- `src/evals/wallet_app_scenarios.py`: ten versioned scenarios, deterministic model/provider/chain fakes, real app/graph factory, lifecycle driver, invariant evaluation, and scenario execution.
- `src/evals/wallet_app_evals.py`: full-suite/single-scenario coordinator, aggregation, JSON CLI, and stable exit codes.
- `src/wallet_agent/api/app.py`: translate a provider registration timeout/error into a sanitized retryable HTTP error without persisting a hash or order.
- `src/wallet_agent/graph/routes.py`, `src/wallet_agent/graph/build.py`: route every approved swap confirmation through allowance authorization before preparation.
- `tests/evals/test_wallet_app_simulator.py`: unit boundary coverage for chunked SSE, fault exhaustion, wallet behavior, redaction, client endpoint mapping, and structured errors.
- `tests/evals/test_wallet_app_lifecycle.py`: public HTTP/SSE lifecycle coverage for all ten scenarios, restart recovery, side-effect ordering, idempotency, bounded progress, report aggregation, and CLI behavior.
- `tests/api/test_swap_flow.py`: focused public API regression for provider registration timeout followed by same-hash retry.
- `tests/api/test_swap_authorization.py`: focused regressions for select → confirm → allowance/approval ordering.
- `README.md`: add the Phase 2 offline command to the project verification section.
- `evals/README.md`: document scenario IDs, safety boundary, report schema, reproduction commands, and exit codes.

---

### Task 1: Add deterministic simulator primitives and sanitized evidence

**Files:**
- Create: `src/evals/wallet_app_simulator.py`
- Create: `tests/evals/test_wallet_app_simulator.py`

**Interfaces:**
- Consumes: standard-library `dataclasses`, `collections.abc.AsyncIterable`, JSON, and HTTPX types only; no production configuration.
- Produces: `SseEvent`, `EvaluationHttpError`, `FaultOutcome`, `FaultSequence`, `FaultPlan`, `EvidenceLedger`, `LifecycleStep`, `InvariantResult`, `LifecycleReport`, `sanitize_evidence(value)`, `parse_sse(chunks)`, and `Eip1193WalletSimulator.request(method, params)`.
- `FaultSequence.next()` consumes configured outcomes once, then returns its explicit fallback; it never silently repeats the last outcome.
- `Eip1193WalletSimulator` supports exactly `eth_chainId`, `eth_accounts`, `wallet_switchEthereumChain`, `wallet_addEthereumChain`, and `eth_sendTransaction`.

- [ ] **Step 1: Write failing SSE, fault, redaction, and wallet tests**

Create `tests/evals/test_wallet_app_simulator.py` with these boundary tests:

```python
import json

import pytest

from evals.wallet_app_simulator import (
    Eip1193Error,
    Eip1193WalletSimulator,
    FaultOutcome,
    FaultSequence,
    parse_sse,
    sanitize_evidence,
)


async def chunks(*values: bytes):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_sse_parser_handles_split_and_multiple_events_and_final_buffer():
    events = [
        event
        async for event in parse_sse(
            chunks(
                b"event: update\ndata: {\"n\":",
                b"1}\n\nevent: action_required\ndata: {\"stage\":\"confirm\"}\n\n",
                b"data: {\"tail\":true}",
            )
        )
    ]

    assert [(event.name, event.data) for event in events] == [
        ("update", {"n": 1}),
        ("action_required", {"stage": "confirm"}),
        ("message", {"tail": True}),
    ]


def test_fault_sequence_consumes_order_then_uses_explicit_fallback():
    sequence = FaultSequence(
        outcomes=(FaultOutcome("not_found"), FaultOutcome("confirmed", {"status": "0x1"})),
        fallback=FaultOutcome("rpc_error", message="exhausted"),
    )

    assert [sequence.next().kind for _ in range(3)] == [
        "not_found",
        "confirmed",
        "rpc_error",
    ]


def test_evidence_redaction_is_recursive_without_hiding_erc20_token_metadata():
    evidence = sanitize_evidence(
        {
            "authorization": "Bearer secret",
            "metadata": {"private_key": "0xdead", "apiKey": "provider-secret"},
            "token": {"symbol": "USDC", "address": "0x" + "3" * 40},
            "data": "0x1234567890abcdef",
        }
    )

    assert evidence == {
        "authorization": "[REDACTED]",
        "metadata": {"private_key": "[REDACTED]", "apiKey": "[REDACTED]"},
        "token": {"symbol": "USDC", "address": "0x" + "3" * 40},
        "data": {"selector": "0x12345678", "length": 18},
    }
    assert "provider-secret" not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_wallet_switches_chain_and_returns_configured_unsigned_transaction_hash():
    tx_hash = "0x" + "a" * 64
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        send_outcomes=FaultSequence(
            outcomes=(FaultOutcome("success", tx_hash),),
            fallback=FaultOutcome("rpc_error", message="unexpected second send"),
        ),
    )

    await wallet.request("wallet_switchEthereumChain", [{"chainId": "0x38"}])
    result = await wallet.request(
        "eth_sendTransaction",
        [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0x", "value": "0x0"}],
    )

    assert result == tx_hash
    assert wallet.chain_id == "0x38"
    assert [call["method"] for call in wallet.calls] == [
        "wallet_switchEthereumChain",
        "eth_sendTransaction",
    ]


@pytest.mark.asyncio
async def test_wallet_rejects_signing_material_and_reports_eip1193_user_rejection():
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        send_outcomes=FaultSequence(
            outcomes=(FaultOutcome("reject", code=4001, message="User rejected request"),),
            fallback=FaultOutcome("reject", code=4001, message="User rejected request"),
        ),
    )

    with pytest.raises(ValueError, match="signed transaction material"):
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "rawTransaction": "0xsigned"}],
        )
    with pytest.raises(Eip1193Error) as rejected:
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0x"}],
        )

    assert rejected.value.code == 4001
    assert sanitize_evidence(wallet.calls)[-1]["params"][0]["to"] == "0x" + "2" * 40


@pytest.mark.asyncio
async def test_wallet_models_switch_failure_and_account_or_chain_change_before_send():
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        switch_outcomes=FaultSequence(
            outcomes=(FaultOutcome("switch_error", code=4902, message="unknown chain"),),
            fallback=FaultOutcome("success"),
        ),
        send_outcomes=FaultSequence(
            outcomes=(
                FaultOutcome("account_change", "0x" + "9" * 40),
                FaultOutcome("chain_change", "0x38"),
            ),
            fallback=FaultOutcome("rpc_error", message="unexpected send"),
        ),
    )

    with pytest.raises(Eip1193Error) as switch_error:
        await wallet.request("wallet_switchEthereumChain", [{"chainId": "0x38"}])
    assert switch_error.value.code == 4902

    with pytest.raises(Eip1193Error, match="account changed"):
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0x"}],
        )
    assert await wallet.request("eth_accounts") == ["0x" + "9" * 40]

    with pytest.raises(Eip1193Error, match="chain changed"):
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "9" * 40, "to": "0x" + "2" * 40, "data": "0x"}],
        )
    assert await wallet.request("eth_chainId") == "0x38"
```

- [ ] **Step 2: Run the focused tests and observe the missing module failure**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_simulator.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'evals.wallet_app_simulator'`.

- [ ] **Step 3: Implement immutable outcomes, report values, and recursive sanitization**

Create `src/evals/wallet_app_simulator.py` with these exact public shapes:

```python
from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SseEvent:
    name: str
    data: dict[str, Any]


@dataclass(frozen=True)
class FaultOutcome:
    kind: str
    value: Any = None
    code: int | str | None = None
    message: str | None = None


@dataclass
class FaultSequence:
    outcomes: tuple[FaultOutcome, ...] = ()
    fallback: FaultOutcome = FaultOutcome("raise", message="fault sequence exhausted")
    _index: int = field(default=0, init=False, repr=False)

    def next(self) -> FaultOutcome:
        if self._index >= len(self.outcomes):
            return self.fallback
        outcome = self.outcomes[self._index]
        self._index += 1
        return outcome


@dataclass(frozen=True)
class FaultPlan:
    wallet_switch: tuple[FaultOutcome, ...] = ()
    wallet_send: tuple[FaultOutcome, ...] = ()
    chain_receipts: tuple[FaultOutcome, ...] = ()
    chain_transactions: tuple[FaultOutcome, ...] = ()
    chain_statuses: tuple[FaultOutcome, ...] = ()
    allowances: tuple[FaultOutcome, ...] = ()
    provider_prepare: tuple[FaultOutcome, ...] = ()
    provider_register: tuple[FaultOutcome, ...] = ()
    provider_status: tuple[FaultOutcome, ...] = ()


@dataclass
class EvidenceLedger:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, actor: str, operation: str, **evidence: Any) -> dict[str, Any]:
        event = sanitize_evidence(
            {
                "sequence": len(self.events) + 1,
                "actor": actor,
                "operation": operation,
                **evidence,
            }
        )
        self.events.append(event)
        return event


@dataclass(frozen=True)
class LifecycleStep:
    operation: str
    stage_before: str | None
    stage_after: str | None
    http_status: int | None
    error_code: str | None
    evidence: dict[str, Any]


@dataclass(frozen=True)
class InvariantResult:
    name: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleReport:
    id: str
    status: str
    final_stage: str
    steps: tuple[LifecycleStep, ...]
    wallet_calls: tuple[dict[str, Any], ...]
    provider_calls: tuple[dict[str, Any], ...]
    invariants: tuple[InvariantResult, ...]
    failures: tuple[str, ...]
    dimensions: tuple[str, ...] = ()

    def model_dump(self) -> dict[str, Any]:
        from dataclasses import asdict

        return sanitize_evidence(asdict(self))
```

Define forbidden canonical keys as `authorization`, `apikey`, `clientsecret`, `credential`, `mnemonic`, `password`, `privatekey`, `rawtransaction`, `seed`, `seedphrase`, `signature`, `signedrawtransaction`, `signer`, and `walletclient`. `sanitize_evidence` must recursively replace those values with `[REDACTED]`, preserve the legitimate `token` asset key, and summarize any calldata string longer than ten characters as selector plus string length.

- [ ] **Step 4: Implement chunk-safe SSE parsing**

Add an async parser that normalizes CRLF, handles comment lines, joins multiple `data:` lines with `\n`, defaults the event name to `message`, JSON-decodes object payloads, wraps non-object JSON as `{"value": value}`, and flushes a final unterminated event:

```python
async def parse_sse(chunks: AsyncIterable[bytes]) -> AsyncIterator[SseEvent]:
    buffer = ""

    async def decode(block: str) -> SseEvent | None:
        name = "message"
        data_lines: list[str] = []
        for line in block.replace("\r\n", "\n").split("\n"):
            if not line or line.startswith(":"):
                continue
            field_name, _, raw_value = line.partition(":")
            value = raw_value[1:] if raw_value.startswith(" ") else raw_value
            if field_name == "event":
                name = value
            elif field_name == "data":
                data_lines.append(value)
        if not data_lines:
            return None
        decoded = json.loads("\n".join(data_lines))
        return SseEvent(name=name, data=decoded if isinstance(decoded, dict) else {"value": decoded})

    async for chunk in chunks:
        buffer += chunk.decode("utf-8")
        normalized = buffer.replace("\r\n", "\n")
        while "\n\n" in normalized:
            block, normalized = normalized.split("\n\n", 1)
            event = await decode(block)
            if event is not None:
                yield event
        buffer = normalized
    if buffer.strip():
        event = await decode(buffer)
        if event is not None:
            yield event
```

- [ ] **Step 5: Implement the strict EIP-1193 wallet simulator**

Add `Eip1193Error(code, message)` and `Eip1193WalletSimulator`. The simulator accepts `switch_outcomes`, `send_outcomes`, and an optional shared `EvidenceLedger`; it must record sanitized `{sequence, actor="wallet", method, params}` entries; normalize chain IDs to hex; update the account/chain only for configured outcomes; reject unsupported methods with code `4200`; reject transaction keys `privateKey`, `signature`, `raw`, `rawTransaction`, and `signedRawTransaction`; and consume one outcome per state-changing wallet call. A `switch_error` raises the configured EIP-1193 error without changing chain. `account_change` and `chain_change` update public wallet state immediately before signing, then raise codes `4100` and `4901` respectively so the driver never submits a stale-account/stale-chain hash.

Use this method boundary:

```python
class Eip1193WalletSimulator:
    def __init__(
        self,
        *,
        chain_id: str,
        accounts: tuple[str, ...],
        switch_outcomes: FaultSequence | None = None,
        send_outcomes: FaultSequence | None = None,
        ledger: EvidenceLedger | None = None,
    ) -> None:
        self.chain_id = hex(int(chain_id, 0))
        self.accounts = tuple(accounts)
        self.switch_outcomes = switch_outcomes or FaultSequence(
            fallback=FaultOutcome("success")
        )
        self.send_outcomes = send_outcomes or FaultSequence(
            fallback=FaultOutcome("rpc_error", message="wallet send outcome not configured")
        )
        self.ledger = ledger or EvidenceLedger()
        self.calls: list[dict[str, Any]] = []

    def _record(self, method: str, params: Sequence[Any]) -> None:
        event = self.ledger.record("wallet", method, method=method, params=list(params))
        self.calls.append(event)

    async def request(self, method: str, params: Sequence[Any] | None = None) -> Any:
        if method == "eth_chainId":
            self._record(method, params or [])
            return self.chain_id
        if method == "eth_accounts":
            self._record(method, params or [])
            return list(self.accounts)
        if method == "wallet_switchEthereumChain":
            outcome = self.switch_outcomes.next()
            self._record(method, params or [])
            if outcome.kind == "switch_error":
                raise Eip1193Error(outcome.code or 4902, outcome.message or "chain switch failed")
            self.chain_id = str((params or [])[0]["chainId"]).lower()
            return None
        if method == "wallet_addEthereumChain":
            self._record(method, params or [])
            return None
        if method != "eth_sendTransaction":
            self._record(method, params or [])
            raise Eip1193Error(4200, f"Unsupported wallet method: {method}")
        transaction = dict((params or [])[0])
        forbidden = {"privatekey", "signature", "raw", "rawtransaction", "signedrawtransaction"}
        if any("".join(c for c in key.lower() if c.isalnum()) in forbidden for key in transaction):
            self._record(method, [transaction])
            raise ValueError("signed transaction material is forbidden")
        self._record(method, [transaction])
        outcome = self.send_outcomes.next()
        if outcome.kind == "success":
            return str(outcome.value)
        if outcome.kind == "account_change":
            self.accounts = (str(outcome.value),)
            raise Eip1193Error(4100, "wallet account changed before signing")
        if outcome.kind == "chain_change":
            self.chain_id = str(outcome.value).lower()
            raise Eip1193Error(4901, "wallet chain changed before signing")
        raise Eip1193Error(outcome.code or 4001, outcome.message or outcome.kind)
```

- [ ] **Step 6: Run simulator tests and lint**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_simulator.py
.venv/bin/ruff check src/evals/wallet_app_simulator.py tests/evals/test_wallet_app_simulator.py
```

Expected: all tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 7: Commit the simulator primitives**

```bash
git add src/evals/wallet_app_simulator.py tests/evals/test_wallet_app_simulator.py
git commit -m "test: add wallet app simulator primitives"
```

---

### Task 2: Add a stateful client that uses only public HTTP and SSE contracts

**Files:**
- Modify: `src/evals/wallet_app_simulator.py`
- Modify: `tests/evals/test_wallet_app_simulator.py`

**Interfaces:**
- Consumes: Task 1 `SseEvent`, `LifecycleStep`, `parse_sse`, `sanitize_evidence`, and `httpx.AsyncClient`.
- Produces: `WalletAppClient(http, user_id, address, chain, conversation_id=None, session_id=None, run_id=None)`, `WalletAppClient.restore(...)`, and async methods `turn`, `session`, `select_quote`, `confirm`, `submit_approval_hash`, `continue_swap`, `submit_swap_hash`, and `transaction_status`.
- Every non-2xx response raises `EvaluationHttpError(operation, status_code, code, message, details)` with sanitized details.

- [ ] **Step 1: Add failing client contract and restart tests**

Append `import httpx` and imports for `EvaluationHttpError` and `WalletAppClient` to `tests/evals/test_wallet_app_simulator.py`. Then add tests that use `httpx.MockTransport` as the external HTTP boundary and return complete production-shaped payloads. Assert this exact request sequence:

```python
@pytest.mark.asyncio
async def test_wallet_app_client_drives_public_routes_and_can_be_reconstructed():
    requests = []

    async def handler(request):
        requests.append((request.method, request.url.path, request.url.params, request.content))
        if request.url.path == "/v1/agent/turn":
            return httpx.Response(
                200,
                json={"run_id": "run-1", "conversation_id": "conversation-1", "session_id": "session-1", "status": "running"},
            )
        if request.url.path == "/v1/agent/stream/run-1":
            return httpx.Response(
                200,
                content=b"event: update\ndata: {\"event\":\"update\",\"data\":{\"stage\":\"selecting_quote\"}}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == "/v1/swap/session-1":
            return httpx.Response(200, json={"session_id": "session-1", "stage": "selecting_quote"})
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as http:
        client = WalletAppClient(http, user_id="alice", address="0x" + "1" * 40, chain="ETH")
        events = await client.turn("swap")
        restored = WalletAppClient.restore(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id=client.conversation_id,
            session_id=client.session_id,
        )
        session = await restored.session()

    assert events[0].name == "update"
    assert (client.conversation_id, client.session_id, client.run_id) == (
        "conversation-1",
        "session-1",
        "run-1",
    )
    assert session["stage"] == "selecting_quote"
    assert [(method, path) for method, path, _, _ in requests] == [
        ("POST", "/v1/agent/turn"),
        ("GET", "/v1/agent/stream/run-1"),
        ("GET", "/v1/swap/session-1"),
    ]


@pytest.mark.asyncio
async def test_wallet_app_client_preserves_structured_public_http_errors():
    async def handler(_request):
        return httpx.Response(
            409,
            json={
                "code": "TRANSACTION_FAILED",
                "message": "chain receipt reverted",
                "details": {"tx_hash": "0x" + "f" * 64, "authorization": "secret"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as http:
        client = WalletAppClient.restore(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id="conversation-1",
            session_id="session-1",
        )
        with pytest.raises(EvaluationHttpError) as raised:
            await client.submit_swap_hash("ETH", "0x" + "f" * 64)

    assert raised.value.code == "TRANSACTION_FAILED"
    assert raised.value.status_code == 409
    assert raised.value.details["authorization"] == "[REDACTED]"
```

- [ ] **Step 2: Run the two client tests and verify the import/attribute failures**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_simulator.py -k 'client'
```

Expected: tests fail because `WalletAppClient` and `EvaluationHttpError` do not exist.

- [ ] **Step 3: Implement structured response handling and evidence recording**

Add:

```python
class EvaluationHttpError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        self.code = code
        self.details = sanitize_evidence(dict(details or {}))
        super().__init__(message)


async def _response_json(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise EvaluationHttpError(
            operation=operation,
            status_code=response.status_code,
            code="INVALID_JSON_RESPONSE",
            message="endpoint returned non-JSON content",
            details={},
        ) from exc
    if response.is_error:
        raise EvaluationHttpError(
            operation=operation,
            status_code=response.status_code,
            code=str(body.get("code", f"HTTP_{response.status_code}")),
            message=str(body.get("message", "request failed")),
            details=body.get("details") if isinstance(body.get("details"), Mapping) else {},
        )
    return dict(body)
```

The client stores `steps: list[LifecycleStep]`. Each method records operation, stage before/after, HTTP status, public error code, and sanitized response evidence. On error, record the failed step before re-raising.

- [ ] **Step 4: Implement all public client operations**

Implement the exact request mapping:

```python
async def turn(self, message: str) -> list[SseEvent]:
    metadata = dict(self.turn_metadata)
    response = await self.http.post(
        "/v1/agent/turn",
        json={
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "message": message,
            "address": self.address,
            "chain": self.chain,
            "metadata": metadata,
        },
    )
    body = await _response_json(response, "turn")
    self.turn_metadata.clear()
    self.run_id = str(body["run_id"])
    self.conversation_id = str(body["conversation_id"])
    self.session_id = str(body["session_id"]) if body.get("session_id") else self.session_id
    async with self.http.stream("GET", f"/v1/agent/stream/{self.run_id}") as stream:
        if stream.is_error:
            await stream.aread()
            await _response_json(stream, "stream")
        events = [event async for event in parse_sse(stream.aiter_bytes())]
    self.events.extend(events)
    return events

async def session(self) -> dict[str, Any]:
    return await self._json("session", "GET", f"/v1/swap/{self._require_session()}", params={"user_id": self.user_id})

async def select_quote(self, provider_reference: str) -> dict[str, Any]:
    return await self._json("select_quote", "POST", f"/v1/swap/{self._require_session()}/select-quote", json={"user_id": self.user_id, "provider_reference": provider_reference})

async def confirm(self, approved: bool) -> dict[str, Any]:
    return await self._json("confirm", "POST", f"/v1/swap/{self._require_session()}/confirm", json={"user_id": self.user_id, "approved": approved})

async def submit_approval_hash(self, chain: str, tx_hash: str) -> dict[str, Any]:
    return await self._json("approve_broadcast", "POST", f"/v1/swap/{self._require_session()}/approve-broadcast", json={"user_id": self.user_id, "chain": chain, "approve_tx_hash": tx_hash})

async def continue_swap(self) -> dict[str, Any]:
    return await self._json("continue", "POST", f"/v1/swap/{self._require_session()}/continue", json={"user_id": self.user_id})

async def submit_swap_hash(self, chain: str, tx_hash: str) -> dict[str, Any]:
    return await self._json("broadcast", "POST", f"/v1/swap/{self._require_session()}/broadcast", json={"user_id": self.user_id, "chain": chain, "tx_hash": tx_hash})

async def transaction_status(self, chain: str, tx_hash: str) -> dict[str, Any]:
    return await self._json("transaction_status", "GET", f"/v1/transactions/{chain}/{tx_hash}", params={"user_id": self.user_id})
```

`restore` must require the caller to provide only the shared `http` transport and public identifiers; it must not accept graph, app, store, checkpoint, or old client fields.

- [ ] **Step 5: Run the complete simulator/client test file**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_simulator.py
.venv/bin/ruff check src/evals/wallet_app_simulator.py tests/evals/test_wallet_app_simulator.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit the public Wallet App client**

```bash
git add src/evals/wallet_app_simulator.py tests/evals/test_wallet_app_simulator.py
git commit -m "test: add public wallet app lifecycle client"
```

---

### Task 3: Enforce select → confirm → allowance/prepare ordering

**Files:**
- Modify: `src/wallet_agent/api/app.py:844-1095`
- Modify: `src/wallet_agent/graph/routes.py:69-76`
- Modify: `src/wallet_agent/graph/build.py:103-116`
- Modify: `tests/api/test_swap_authorization.py`
- Modify: `tests/api/test_swap_flow.py`

**Interfaces:**
- Consumes: the existing internal `swap_select` intent, `confirmation_request`, the `confirmation_required` LangGraph interrupt, `ConfirmRequest`, `swap_allowance`, and the existing session projection.
- Produces: all swaps follow `quote selected -> awaiting_confirmation -> approved -> swap_allowance -> approval_required|swap_ready`; provider `prepare` and allowance reads cannot occur before explicit confirmation.

- [ ] **Step 1: Write failing multi-quote confirmation/allowance ordering tests**

Replace the old immediate-approval expectation in `test_select_quote_returns_approval_and_continue_prepares_swap` with this contract, using a local `CountingAdapter(Adapter)` whose `get_allowance` increments `allowance_calls` before delegating:

```python
@pytest.mark.asyncio
async def test_select_quote_requires_confirmation_before_allowance_and_prepare():
    quote = authorization_quote("ref-confirm-first")

    class CountingAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.allowance_calls = 0

        async def get_allowance(self, token, owner, spender):
            self.allowance_calls += 1
            return await super().get_allowance(token, owner, spender)

    adapter = CountingAdapter()
    provider = Provider()
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-confirm-first",
            user_id="alice",
            thread_id="t-confirm-first",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=Model(), providers=[provider], chains={"BASE": adapter})
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        chain_registry=ChainAdapterRegistry({"BASE": adapter}),
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        selected = await client.post(
            "/v1/swap/s-confirm-first/select-quote",
            json={"user_id": "alice", "provider_reference": quote.provider_reference},
        )
        assert selected.status_code == 200
        assert selected.json()["status"] == "awaiting_confirmation"
        assert selected.json()["approval_transaction"] is None
        assert adapter.allowance_calls == 0
        assert provider.prepare_calls == 0

        confirmed = await client.post(
            "/v1/swap/s-confirm-first/confirm",
            json={"user_id": "alice", "approved": True},
        )

    assert confirmed.status_code == 200
    assert confirmed.json()["stage"] == "approval_required"
    assert confirmed.json()["approval_transaction"]["data"].startswith("0x095ea7b3")
    assert adapter.allowance_calls == 1
    assert provider.prepare_calls == 0
```

Also add this no-approval quote test to `tests/api/test_swap_flow.py`:

```python
@pytest.mark.asyncio
async def test_select_quote_requires_confirmation_before_no_approval_prepare():
    provider = FakeProvider()
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=Asset(
            chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x" + "1" * 40
        ),
        destination_asset=Asset(
            chain="BSC", chain_id=56, symbol="USDT", decimals=6, address="0x" + "2" * 40
        ),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        expected_output=Decimal("9"),
        expected_output_raw="9000000",
        provider_reference="quote-confirm-first",
    )
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-no-approval-confirm",
            user_id="alice",
            thread_id="t-no-approval-confirm",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=FakeModel(), providers=[provider])
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        selected = await client.post(
            "/v1/swap/s-no-approval-confirm/select-quote",
            json={"user_id": "alice", "provider_reference": quote.provider_reference},
        )
        assert selected.status_code == 200
        assert selected.json()["status"] == "awaiting_confirmation"
        assert provider.prepare_calls == 0

        confirmed = await client.post(
            "/v1/swap/s-no-approval-confirm/confirm",
            json={"user_id": "alice", "approved": True},
        )

    assert confirmed.status_code == 200
    assert confirmed.json()["stage"] == "swap_ready"
    assert provider.prepare_calls == 1
```

Add `SwapSessionRecord` to that test file's existing persistence imports.

- [ ] **Step 2: Run the new ordering tests and observe both wrong branches**

Run:

```bash
.venv/bin/pytest -q tests/api/test_swap_authorization.py -k confirmation_before_allowance
.venv/bin/pytest -q tests/api/test_swap_flow.py -k select_quote_requires_confirmation
```

Expected: the authorization test fails because selection immediately returns `approval_transaction`; the no-approval test fails because selection immediately calls `provider.prepare`.

- [ ] **Step 3: Route approved confirmation into allowance authorization**

Change `route_after_confirmation` in `src/wallet_agent/graph/routes.py`:

```python
def route_after_confirmation(state: dict[str, Any]) -> str:
    if state.get("response_action") == "reparse":
        return "supervisor"
    confirmation = state.get("confirmation_state") or {}
    if confirmation.get("status") == "approved":
        return "swap_allowance"
    return "response"
```

Update the `confirmation_wait` conditional map in `src/wallet_agent/graph/build.py` from `{"prepare": "prepare", ...}` to:

```python
{"swap_allowance": "swap_allowance", "response": "response", "supervisor": "supervisor"}
```

Also split the current combined intent branch in `route_after_intent`:

```python
if intent == "swap_select":
    return "confirmation_request"
if intent == "swap_allowance":
    return "swap_allowance"
```

Add `"confirmation_request": "confirmation_request"` to the `route_after_intent` conditional-edge map in `build_graph`. This reuses the already declared `swap_select` state literal and avoids reconstructing a missing `swap_request` when API-focused tests start from a persisted quote candidate.

- [ ] **Step 4: Make explicit selection enter the existing confirmation interrupt**

In `/v1/swap/{session_id}/select-quote`, invoke the graph with `swap_select` rather than `swap_allowance`:

```python
async with graph_lock(session.thread_id):
    result = await app.state.graph.ainvoke(
        {
            "intent": "swap_select",
            "forced_intent": "swap_select",
            "selected_quote": selected_quote.model_dump(mode="json"),
        },
        config={"configurable": {"thread_id": session.thread_id}},
    )
updated = await project_session(session_id, result, status="awaiting_confirmation")
if updated is not None:
    updated = await session_store.update(
        session_id,
        status="awaiting_confirmation",
        stage="confirmation_required",
        selected_provider_reference=payload.provider_reference,
    )
return _jsonable(updated or result)
```

Delete the select endpoint's current fallback that constructs an approval transaction. Selection must not read allowance, construct approval calldata, or prepare the swap.

- [ ] **Step 5: Persist approval interrupt data only after confirmation**

Move the removed approval projection logic into this local `create_app` helper:

```python
async def persist_approval_projection(
    session_id: str,
    session: SwapSessionRecord,
    quote: NormalizedQuote,
) -> SwapSessionRecord:
    requirement = quote.allowance_requirement
    if requirement is None or app.state.chain_registry is None:
        return session
    adapter = app.state.chain_registry.get_adapter(requirement.token.chain)
    tx = adapter.build_erc20_approve(
        token=requirement.token,
        owner=requirement.owner,
        spender=requirement.spender,
        amount_raw=requirement.required_amount_raw,
    )
    approval = {
        "chain": requirement.token.chain,
        "chain_id": requirement.token.chain_id,
        "to": tx.to,
        "data": tx.data,
        "value": tx.value,
        "token": requirement.token.model_dump(mode="json"),
        "owner": requirement.owner,
        "spender": requirement.spender,
        "amount_raw": requirement.required_amount_raw,
        "expires_at": None,
    }
    return await session_store.update(
        session_id,
        status="approval_required",
        stage="approval_required",
        approval_transaction=approval,
        allowance_requirement=requirement.model_dump(mode="json"),
    )
```

After `confirm` invokes `Command(resume={"approved": True})`, read the graph snapshot under the same thread lock. If `_has_approval_interrupt(snapshot)` is true, project the returned state, then call `persist_approval_projection(session_id, updated, session.quote)`. Otherwise use `authorization_stage` as both status and stage when present; this makes the no-approval result `swap_ready` rather than generic `prepared`/`confirmed`.

- [ ] **Step 6: Update existing authorization tests to perform explicit confirmation**

Add this test-only helper to `tests/api/test_swap_authorization.py`:

```python
async def select_and_confirm(client, session_id: str, provider_reference: str):
    selected = await client.post(
        f"/v1/swap/{session_id}/select-quote",
        json={"user_id": "alice", "provider_reference": provider_reference},
    )
    assert selected.status_code == 200
    assert selected.json()["status"] == "awaiting_confirmation"
    return await client.post(
        f"/v1/swap/{session_id}/confirm",
        json={"user_id": "alice", "approved": True},
    )
```

Replace each setup sequence in that file that currently expects approval immediately from `select-quote` with `await select_and_confirm(...)`. Keep the later `/approve-broadcast` and `/continue` calls unchanged. Do not use this helper in the new confirmation-order test because that test must inspect the intermediate selected state.

- [ ] **Step 7: Run graph, API, and authorization regressions**

Run:

```bash
.venv/bin/pytest -q tests/api/test_swap_authorization.py tests/api/test_swap_flow.py tests/graph/test_graph_paths.py tests/graph/test_resume.py
.venv/bin/ruff check src/wallet_agent/api/app.py src/wallet_agent/graph tests/api/test_swap_authorization.py tests/api/test_swap_flow.py
```

Expected: explicit selection and automatic single-quote continuation both stop at confirmation; approval is offered only after confirmation; allowance-sufficient swaps become `swap_ready`; all selected tests pass.

- [ ] **Step 8: Commit the lifecycle ordering fix**

```bash
git add src/wallet_agent/api/app.py src/wallet_agent/graph/routes.py src/wallet_agent/graph/build.py tests/api/test_swap_authorization.py tests/api/test_swap_flow.py
git commit -m "fix: confirm swaps before allowance and preparation"
```

---

### Task 4: Build the real app scenario harness and happy-path lifecycle coverage

**Files:**
- Create: `src/evals/wallet_app_scenarios.py`
- Create: `tests/evals/test_wallet_app_lifecycle.py`

**Interfaces:**
- Consumes: `build_graph`, `create_app`, `ChainAdapterRegistry`, `InMemorySessionStore`, production domain models, `ASGITransport`, Task 1/2 simulator primitives, and only deterministic fixed model output.
- Produces: frozen `ScenarioDefinition`, `ScenarioRuntime`, `SCENARIOS`, `build_scenario_runtime(definition)`, `wallet_transaction(unsigned, from_address)`, `drive_scenario(runtime, max_attempts=3)`, and `run_scenario(scenario_id, max_attempts=3)`.
- `build_scenario_runtime` may construct and inject graph/app/fakes. `drive_scenario` and lifecycle tests receive only client/wallet/public evidence handles and never access `app.state`, graph methods, or the store.

- [ ] **Step 1: Add failing no-approval and approval happy-path tests**

Create `tests/evals/test_wallet_app_lifecycle.py`:

```python
import pytest

from evals.wallet_app_scenarios import run_scenario


@pytest.mark.parametrize("scenario_id", ["erc20_swap_without_approval", "erc20_swap_with_approval"])
@pytest.mark.asyncio
async def test_happy_paths_complete_through_public_wallet_app_contract(scenario_id):
    report = await run_scenario(scenario_id)

    assert report.status == "passed"
    assert report.final_stage == "completed"
    assert report.failures == ()
    assert all(result.passed for result in report.invariants)


@pytest.mark.asyncio
async def test_approval_happy_path_orders_wallet_and_provider_side_effects():
    report = await run_scenario("erc20_swap_with_approval")

    send_calls = [call for call in report.wallet_calls if call["method"] == "eth_sendTransaction"]
    register_calls = [call for call in report.provider_calls if call["operation"] == "register_broadcast"]
    assert len(send_calls) == 2
    assert send_calls[0]["params"][0]["to"] == "0x" + "3" * 40
    assert send_calls[1]["params"][0]["to"] == "0x" + "5" * 40
    assert len(register_calls) == 1
    assert register_calls[0]["tx_hash"] == "0x" + "b" * 64
    assert [step.operation for step in report.steps] == [
        "turn",
        "stream",
        "session",
        "select_quote",
        "confirm",
        "wallet_approval",
        "approve_broadcast",
        "continue",
        "wallet_swap",
        "broadcast",
        "status_turn",
        "stream",
        "session",
    ]
```

- [ ] **Step 2: Run focused lifecycle tests and observe the missing scenario module**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py -k 'happy_path or approval_happy'
```

Expected: collection fails because `evals.wallet_app_scenarios` does not exist.

- [ ] **Step 3: Define literal scenario data and production-shaped deterministic fakes**

Create these immutable definitions in `src/evals/wallet_app_scenarios.py`:

```python
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
```

Implement `FixedSwapModel` as a deterministic turn-aware model: its first `ainvoke` returns `{"intent": "swap_quote"}` and later invocations return `{"intent": "swap_status"}`. The initial `turn_metadata` is one-shot (Task 2 clears it after posting), so a later status turn resumes the public session without resubmitting the original structured swap request. Implement `RecordingChainAdapter` with the complete methods consumed by the graph/API: `get_allowance`, `build_erc20_approve`, `estimate_fee`, `get_transaction_receipt`, `get_transaction`, and `get_transaction_status`. Implement `RecordingProvider` with `list_assets`, `quote`, `prepare`, `register_broadcast`, and `get_status`; return production `Asset`, `NormalizedQuote`, `UnsignedTransaction`, `ProviderOrder`, and `NormalizedOrderStatus` objects with literal values. Each fake records `{sequence, actor, operation, sanitized arguments, outcome}` through the same `EvidenceLedger` passed to the wallet, and consumes a private `FaultSequence` cloned from the frozen plan. This shared monotonic sequence is the evidence for register-after-wallet-hash ordering.

Implement all declared outcome kinds at the fake boundary with these literal mappings:

- receipt `not_found`/`pending` returns `None`; `confirmed`/`reverted` returns the supplied receipt; `rpc_error` raises `RuntimeError(message)`;
- transaction `not_found` returns `None`; `visible` returns `{"hash": tx_hash}`; `rpc_error` raises;
- status `pending`/`confirmed`/`failed`/`dropped` returns the matching `TransactionStatus` enum; `rpc_error` raises;
- allowance `insufficient`/`sufficient` returns the supplied raw integer string; `read_error` raises;
- provider prepare `success` returns the scenario transaction/deposit order; `error` raises; `malformed` returns the supplied invalid value so production validation owns the failure;
- provider register `success` returns one `ProviderOrder`; `error` and `timeout` raise `RuntimeError` and `TimeoutError` respectively without an order ID;
- provider status `processing`/`completed`/`failed`/`refunded`/`timed_out` returns a matching `NormalizedOrderStatus`; `error`/`timeout` raises; `malformed` returns the supplied invalid value.

Use two configured quote providers in the happy-path harness so `quote_candidates` has more than one item and the driver must call `/select-quote`; only the scenario's literal `provider_reference` may reach `prepare`.

- [ ] **Step 4: Construct the real compiled graph and in-process FastAPI transport**

Implement the factory in this order:

```python
async def build_scenario_runtime(definition: ScenarioDefinition) -> ScenarioRuntime:
    chain = RecordingChainAdapter(definition)
    primary = RecordingProvider(definition, provider_name=definition.provider)
    alternate_name = "omnibridge" if definition.provider == "bridgers" else "bridgers"
    alternate = RecordingProvider(definition, provider_name=alternate_name, alternate=True)
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
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://wallet.test")
    ledger = EvidenceLedger()
    chain.ledger = primary.ledger = alternate.ledger = ledger
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
    return ScenarioRuntime(definition, http, client, wallet, chain, primary, alternate)
```

No factory may call `Settings`, load `.env`, instantiate `BridgersProvider`, `OmniBridgeProvider`, or a real transport.

- [ ] **Step 5: Implement unsigned transaction conversion and the happy-path driver**

`wallet_transaction` maps only public unsigned fields and converts decimal raw integers to EIP-1193 hex quantities:

```python
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
```

The driver must: call `turn`; consume its returned SSE; fetch `session`; explicitly select the primary `provider_reference`; confirm; sign approval when `approval_transaction` exists; submit the approval hash; call `continue_swap` at most `max_attempts`; sign the prepared swap; submit the swap hash; issue a public status turn using the same session; consume SSE; and fetch the final session. Record wallet actions as lifecycle steps adjacent to HTTP steps.

- [ ] **Step 6: Add initial invariant evaluation and make both happy paths green**

For this task, implement the invariant names `no_signing_material_to_server`, `no_server_wallet_actions`, `no_register_before_wallet_hash`, `explicit_quote_selection`, and `bounded_progress`. Derive them from client steps, the shared ledger, construction manifest, provider evidence, and attempt counts—not from internal graph state. `no_server_wallet_actions` passes only when the construction manifest confirms the wallet object was not injected into `build_graph`/`create_app` and every ledger event whose operation is an EIP-1193 method has `actor="wallet"`.

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py -k 'happy_path or approval_happy'
.venv/bin/ruff check src/evals/wallet_app_scenarios.py tests/evals/test_wallet_app_lifecycle.py
```

Expected: both scenarios pass through the real API/graph and Ruff reports no errors.

- [ ] **Step 7: Commit the scenario harness and happy paths**

```bash
git add src/evals/wallet_app_scenarios.py tests/evals/test_wallet_app_lifecycle.py
git commit -m "test: evaluate wallet app swap happy paths"
```

---

### Task 5: Cover restart, wallet rejection, RPC invisibility, and reverted receipts

**Files:**
- Modify: `src/evals/wallet_app_scenarios.py`
- Modify: `tests/evals/test_wallet_app_lifecycle.py`

**Interfaces:**
- Consumes: Task 4 scenario factory/driver and `WalletAppClient.restore`.
- Produces scenarios `approval_pending_restart_resume`, `wallet_rejects_approval`, `wallet_rejects_swap`, `swap_temporarily_not_visible`, and `swap_reverted`; completes invariants `rejection_stops_side_effects`, `reverted_is_not_success`, `pending_is_recoverable`, and `restart_uses_public_state`.

- [ ] **Step 1: Add five failing lifecycle regressions**

Append literal tests:

```python
@pytest.mark.parametrize(
    ("scenario_id", "final_stage"),
    [
        ("approval_pending_restart_resume", "completed"),
        ("wallet_rejects_approval", "approval_required"),
        ("wallet_rejects_swap", "swap_ready"),
        ("swap_temporarily_not_visible", "completed"),
        ("swap_reverted", "failed"),
    ],
)
@pytest.mark.asyncio
async def test_recovery_and_failure_scenarios_are_safe(scenario_id, final_stage):
    report = await run_scenario(scenario_id)

    assert report.status == "passed"
    assert report.final_stage == final_stage
    assert all(result.passed for result in report.invariants)


@pytest.mark.asyncio
async def test_restart_uses_only_public_session_projection():
    report = await run_scenario("approval_pending_restart_resume")
    operations = [step.operation for step in report.steps]

    assert "client_restart" in operations
    restart = next(step for step in report.steps if step.operation == "client_restart")
    assert set(restart.evidence) == {"conversation_id", "session_id"}
    assert operations.index("client_restart") < operations.index("continue")


@pytest.mark.parametrize("scenario_id", ["wallet_rejects_approval", "wallet_rejects_swap"])
@pytest.mark.asyncio
async def test_wallet_rejection_never_registers_provider_order(scenario_id):
    report = await run_scenario(scenario_id)

    assert [call for call in report.provider_calls if call["operation"] == "register_broadcast"] == []
    assert "wallet_rejected" in [step.error_code for step in report.steps]


@pytest.mark.asyncio
async def test_reverted_swap_is_not_registered_or_reported_successful():
    report = await run_scenario("swap_reverted")

    broadcast = next(step for step in report.steps if step.operation == "broadcast")
    assert broadcast.http_status == 409
    assert broadcast.error_code == "TRANSACTION_FAILED"
    assert [call for call in report.provider_calls if call["operation"] == "register_broadcast"] == []
```

- [ ] **Step 2: Run the five cases and verify unknown-scenario failures**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py -k 'recovery_and_failure or restart_uses or wallet_rejection or reverted_swap'
```

Expected: failures name the five missing `SCENARIOS` entries.

- [ ] **Step 3: Add literal fault plans for all five scenarios**

Add definitions with these ordered outcomes. Give the three non-rejection scenarios all three report dimensions; the two wallet-rejection scenarios retain only `wallet_api_contract` and `safety`, yielding the spec's aggregate `broadcast_and_confirmation.total == 8`:

```python
"approval_pending_restart_resume": FaultPlan(
    allowances=(FaultOutcome("insufficient", "0"), FaultOutcome("sufficient", "10000000")),
    chain_receipts=(FaultOutcome("not_found"), FaultOutcome("pending"), FaultOutcome("confirmed", {"status": "0x1"})),
    provider_register=(FaultOutcome("success", "order-restart"),),
    provider_status=(FaultOutcome("completed"),),
),
"wallet_rejects_approval": FaultPlan(
    allowances=(FaultOutcome("insufficient", "0"),),
    wallet_send=(FaultOutcome("reject", code=4001, message="User rejected approval"),),
),
"wallet_rejects_swap": FaultPlan(
    allowances=(FaultOutcome("sufficient", "10000000"),),
    wallet_send=(FaultOutcome("reject", code=4001, message="User rejected swap"),),
),
"swap_temporarily_not_visible": FaultPlan(
    allowances=(FaultOutcome("sufficient", "10000000"),),
    chain_receipts=(FaultOutcome("not_found"), FaultOutcome("not_found"), FaultOutcome("not_found"), FaultOutcome("confirmed", {"status": "0x1"})),
    chain_transactions=(FaultOutcome("not_found"), FaultOutcome("not_found"), FaultOutcome("not_found")),
    provider_register=(FaultOutcome("success", "order-pending"),),
    provider_status=(FaultOutcome("processing"), FaultOutcome("completed")),
),
"swap_reverted": FaultPlan(
    allowances=(FaultOutcome("sufficient", "10000000"),),
    chain_receipts=(FaultOutcome("reverted", {"status": "0x0"}),),
),
```

When `FaultPlan.wallet_send` is non-empty, use it instead of `wallet_hashes` when creating the wallet. Give every sequence an explicit fallback matching the scenario's stable terminal state.

- [ ] **Step 4: Implement restart, rejection, pending, and reverted branches**

In the approval loop, treat missing/pending receipts as public `approval_pending`, reconstruct the client when `restart_after_approval` is true, and continue within the same attempt limit:

```python
runtime.client = WalletAppClient.restore(
    runtime.http,
    user_id=old.user_id,
    address=old.address,
    chain=old.chain,
    conversation_id=old.conversation_id,
    session_id=old.session_id,
)
session = await runtime.client.session()
```

Record only `conversation_id` and `session_id` as restart evidence. Catch `Eip1193Error(code=4001)` at the wallet boundary, record `wallet_rejected`, stop the scenario without submitting a hash, and leave the server session actionable. Catch `EvaluationHttpError(code="TRANSACTION_FAILED")` as the expected reverted outcome and do not issue status polling or provider registration assertions as success.

For temporary invisibility, accept `broadcast_pending`, retain the submitted hash from the public session, and use bounded public status turns/session reads until the provider status is `completed` or the limit is exhausted.

- [ ] **Step 5: Implement the four new invariants**

Add literal `InvariantResult`s:

- `rejection_stops_side_effects`: after a wallet rejection there is no later hash-submission step and no provider register call.
- `reverted_is_not_success`: a `TRANSACTION_FAILED` broadcast has no provider register call and final stage is `failed`.
- `pending_is_recoverable`: `broadcast_pending` retains the same public hash and a later status operation advances without a second wallet send.
- `restart_uses_public_state`: restart evidence contains exactly public IDs and the next operation is `session` or `continue`.

- [ ] **Step 6: Run recovery/failure tests and the existing API regressions**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py
.venv/bin/pytest -q tests/api/test_swap_authorization.py tests/api/test_swap_flow.py
.venv/bin/ruff check src/evals tests/evals
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit lifecycle recovery and failure coverage**

```bash
git add src/evals/wallet_app_scenarios.py tests/evals/test_wallet_app_lifecycle.py
git commit -m "test: cover wallet lifecycle recovery failures"
```

---

### Task 6: Make provider registration failure structured and safely retryable

**Files:**
- Modify: `src/wallet_agent/api/app.py:985-998`
- Modify: `tests/api/test_swap_flow.py`

**Interfaces:**
- Consumes: existing per-thread broadcast lock, idempotent persisted-hash check, `provider.register_broadcast(provider_reference, tx_hash)`, and FastAPI's stable error envelope.
- Produces: HTTP 503 `PROVIDER_REGISTRATION_FAILED` with sanitized `{provider, tx_hash, retryable: true}` details; no session hash/order is committed on failure, so a retry with the same wallet hash can succeed exactly once.

- [ ] **Step 1: Add the failing public API retry regression**

Append to `tests/api/test_swap_flow.py`:

```python
@pytest.mark.asyncio
async def test_provider_registration_timeout_allows_same_hash_retry_without_duplicate_order():
    class FlakyProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.register_attempts = 0
            self.created_orders = 0

        async def register_broadcast(self, provider_reference, tx_hash):
            self.register_attempts += 1
            if self.register_attempts == 1:
                raise TimeoutError("provider timed out")
            self.created_orders += 1
            return ProviderOrder(
                provider="bridgers",
                provider_order_id="order-after-retry",
                provider_reference=provider_reference,
                tx_hash=tx_hash,
            )

    provider = FlakyProvider()
    graph = build_graph(model=FakeModel(), providers=[provider])
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        store=InMemorySessionStore(),
    )
    tx_hash = "0x" + "f" * 64
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn = await client.post("/v1/agent/turn", json=request_payload())
        await app.state.runs[turn.json()["run_id"]]["task"]
        session_id = turn.json()["session_id"]
        await client.post(
            f"/v1/swap/{session_id}/confirm",
            json={"user_id": "alice", "approved": True},
        )
        first = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash},
        )
        second = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash},
        )
        duplicate = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash},
        )

    assert first.status_code == 503
    assert first.json() == {
        "code": "PROVIDER_REGISTRATION_FAILED",
        "message": "兑换服务暂时无法登记交易，请使用同一交易哈希重试。",
        "details": {"provider": "bridgers", "tx_hash": tx_hash, "retryable": True},
    }
    assert second.status_code == 200
    assert duplicate.status_code == 200
    assert second.json()["broadcast_tx_hash"] == tx_hash
    assert provider.register_attempts == 2
    assert provider.created_orders == 1
```

This focused API test may await `app.state.runs[...]` because it is an API handler regression, not a Wallet App lifecycle test. No test under `tests/evals/test_wallet_app_lifecycle.py` may do so.

- [ ] **Step 2: Run the regression and observe the unhandled timeout**

Run:

```bash
.venv/bin/pytest -q tests/api/test_swap_flow.py -k provider_registration_timeout
```

Expected: FAIL because `TimeoutError("provider timed out")` escapes `provider.register_broadcast` instead of becoming a 503 response.

- [ ] **Step 3: Add the minimal sanitized provider error boundary**

In `src/wallet_agent/api/app.py`, wrap only the provider registration call inside the existing per-thread lock:

```python
try:
    order = await provider.register_broadcast(reference, payload.tx_hash)
except Exception as exc:
    raise HTTPException(
        status_code=503,
        detail={
            "code": "PROVIDER_REGISTRATION_FAILED",
            "message": "兑换服务暂时无法登记交易，请使用同一交易哈希重试。",
            "details": {
                "provider": session.quote.provider,
                "tx_hash": payload.tx_hash,
                "retryable": True,
            },
        },
    ) from exc
```

Do not persist `broadcast_tx_hash`, `provider_order`, or a success stage in the exception branch, and do not expose `str(exc)` because provider exceptions may contain transport or credential details.

- [ ] **Step 4: Run the focused and neighboring broadcast tests**

Run:

```bash
.venv/bin/pytest -q tests/api/test_swap_flow.py -k 'provider_registration_timeout or idempotent or conflicting or temporarily_missing or failed_receipt'
.venv/bin/ruff check src/wallet_agent/api/app.py tests/api/test_swap_flow.py
```

Expected: the first attempt is a structured 503, the same hash succeeds on retry, the duplicate is idempotent, and all neighboring regressions pass.

- [ ] **Step 5: Commit the production retry boundary**

```bash
git add src/wallet_agent/api/app.py tests/api/test_swap_flow.py
git commit -m "fix: make provider registration safely retryable"
```

---

### Task 7: Cover hash retry safety, provider timeout, and Omni deposit ordering

**Files:**
- Modify: `src/evals/wallet_app_scenarios.py`
- Modify: `tests/evals/test_wallet_app_lifecycle.py`

**Interfaces:**
- Consumes: Task 5 lifecycle driver, Task 6 structured provider retry behavior, and production duplicate/conflict behavior in `/broadcast`.
- Produces scenarios `duplicate_and_conflicting_swap_hash`, `provider_register_timeout_then_retry`, and `omnibridge_erc20_deposit_order`; completes invariants `same_hash_is_idempotent` and `conflicting_hash_is_rejected`.

- [ ] **Step 1: Add failing retry, conflict, and Omni tests**

Append:

```python
@pytest.mark.parametrize(
    "scenario_id",
    [
        "duplicate_and_conflicting_swap_hash",
        "provider_register_timeout_then_retry",
        "omnibridge_erc20_deposit_order",
    ],
)
@pytest.mark.asyncio
async def test_retry_and_omni_scenarios_pass_all_invariants(scenario_id):
    report = await run_scenario(scenario_id)

    assert report.status == "passed"
    assert report.failures == ()
    assert all(result.passed for result in report.invariants)


@pytest.mark.asyncio
async def test_duplicate_hash_is_idempotent_and_conflicting_hash_is_rejected():
    report = await run_scenario("duplicate_and_conflicting_swap_hash")
    broadcasts = [step for step in report.steps if step.operation == "broadcast"]
    registrations = [call for call in report.provider_calls if call["operation"] == "register_broadcast"]

    assert [step.http_status for step in broadcasts] == [200, 200, 409]
    assert broadcasts[2].error_code == "HTTP_409"
    assert len(registrations) == 1
    assert registrations[0]["tx_hash"] == "0x" + "d" * 64


@pytest.mark.asyncio
async def test_provider_timeout_retry_commits_one_order_for_same_wallet_hash():
    report = await run_scenario("provider_register_timeout_then_retry")
    attempts = [call for call in report.provider_calls if call["operation"] == "register_broadcast"]

    assert [call["outcome"] for call in attempts] == ["timeout", "success"]
    assert len([call for call in attempts if call.get("provider_order_id")]) == 1
    assert len([call for call in report.wallet_calls if call["method"] == "eth_sendTransaction"]) == 1
    timeout = next(step for step in report.steps if step.error_code == "PROVIDER_REGISTRATION_FAILED")
    assert timeout.http_status == 503
    assert timeout.evidence["retryable"] is True


@pytest.mark.asyncio
async def test_omni_erc20_deposit_uses_order_reference_and_token_transfer():
    report = await run_scenario("omnibridge_erc20_deposit_order")
    wallet_send = next(call for call in report.wallet_calls if call["method"] == "eth_sendTransaction")
    registration = next(call for call in report.provider_calls if call["operation"] == "register_broadcast")

    assert wallet_send["params"][0]["to"] == "0x" + "3" * 40
    assert wallet_send["params"][0]["data"]["selector"] == "0xa9059cbb"
    assert registration["provider_reference"] == "omni-deposit-order-1"
    assert registration["provider_reference"] != "omni-quote-1"
```

- [ ] **Step 2: Run focused tests and verify all three scenarios are missing**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py -k 'retry_and_omni or duplicate_hash or provider_timeout or omni_erc20'
```

Expected: failures identify the missing scenario IDs.

- [ ] **Step 3: Add the three definitions and scenario-local driver actions**

Define:

- `duplicate_and_conflicting_swap_hash`: wallet returns `0x` + `d` × 64; submit it three times as same, same, then `0x` + `e` × 64.
- `provider_register_timeout_then_retry`: wallet returns one `0x` + `f` × 64 hash; provider register outcomes are timeout then success; require the first public response to be HTTP 503 `PROVIDER_REGISTRATION_FAILED`, then retry `/broadcast` with the client-owned hash and never ask the wallet to sign again.
- `omnibridge_erc20_deposit_order`: provider `prepare` returns `DepositOrder(provider="omnibridge", provider_order_id="omni-deposit-order-1", deposit_address="0x" + "4" * 40, source_asset=ETH_USDC, destination_asset=BSC_USDT, input_amount=Decimal("9.5"), input_amount_raw="9500000", recipient_address=WALLET_ADDRESS, refund_address=WALLET_ADDRESS, provider_reference="omni-deposit-order-1")`, while the quote reference is `omni-quote-1`.

Add a `driver_actions: tuple[str, ...]` field with defaults `("broadcast", "status")`; use `("broadcast", "broadcast_same", "broadcast_conflict")` and `("broadcast_timeout", "broadcast_retry", "status")` for the two retry scenarios. This keeps retry expectations literal rather than inferred from scenario names.

- [ ] **Step 4: Make the chain fake produce a real ERC-20 transfer for Omni**

Implement `build_erc20_transfer` using the production-shaped return type and a deterministic selector:

```python
def build_erc20_transfer(self, *, token, from_address, to_address, amount_raw):
    del from_address
    padded_to = to_address.removeprefix("0x").lower().rjust(64, "0")
    padded_amount = hex(int(amount_raw))[2:].rjust(64, "0")
    return UnsignedTransaction(
        chain="ETH",
        chain_id=1,
        to=str(token.address),
        data=f"0xa9059cbb{padded_to}{padded_amount}",
        value="0",
    )
```

Return a fixed `FeeEstimate` so `_deposit_order_to_transaction` fills `gas_limit`, `max_fee_per_gas`, and `max_priority_fee_per_gas`. Assert those three fields exist before asking the wallet to send. Keep the existing focused native-deposit production regression unchanged; this scenario covers ERC-20 deposit ordering only.

- [ ] **Step 5: Add idempotency/conflict invariants and make retry logic bounded**

Implement:

- `same_hash_is_idempotent`: all successful duplicate submissions retain the same hash and exactly one provider order is created.
- `conflicting_hash_is_rejected`: the different hash receives HTTP 409 and the public session still contains the first hash.
- Extend `bounded_progress` so provider retry attempts are also `<= max_attempts`.

Do not count the expected timeout attempt as an order; the fake records `provider_order_id` only on its success outcome.

- [ ] **Step 6: Run all ten scenarios plus focused Omni/API regressions**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py
.venv/bin/pytest -q tests/api/test_swap_flow.py -k 'broadcast or omnibridge'
.venv/bin/ruff check src/evals tests/evals
```

Expected: ten unique scenario IDs pass, duplicate registration remains one, conflict is 409, and Omni uses the deposit order reference.

- [ ] **Step 7: Commit retry and Omni scenario coverage**

```bash
git add src/evals/wallet_app_scenarios.py tests/evals/test_wallet_app_lifecycle.py
git commit -m "test: cover wallet broadcast retry and omni deposit"
```

---

### Task 8: Add stable aggregation, JSON CLI, single-scenario reproduction, and exit codes

**Files:**
- Create: `src/evals/wallet_app_evals.py`
- Modify: `tests/evals/test_wallet_app_lifecycle.py`

**Interfaces:**
- Consumes: `SCENARIOS`, `run_scenario`, and `LifecycleReport.model_dump()`.
- Produces: `run_wallet_app_evals(scenario_id: str | None = None) -> dict[str, Any]`, `exit_code(report) -> int`, `main(argv: Sequence[str] | None = None) -> int`, module execution via `python -m evals.wallet_app_evals`, schema version `1`, and mode `offline-wallet-app`.
- Exit codes: `0` only when all selected scenarios pass; `1` for failed or blocked scenarios; `2` for unknown scenario/usage/evaluator configuration errors.

- [ ] **Step 1: Add failing report aggregation and CLI tests**

Append:

```python
import json

from evals.wallet_app_evals import exit_code, main, run_wallet_app_evals


@pytest.mark.asyncio
async def test_wallet_app_report_has_stable_schema_dimensions_and_ten_scenarios():
    report = await run_wallet_app_evals()

    assert report["schema_version"] == 1
    assert report["mode"] == "offline-wallet-app"
    assert report["summary"] == {"total": 10, "passed": 10, "failed": 0, "blocked": 0}
    assert report["dimensions"]["wallet_api_contract"] == {"total": 10, "passed": 10}
    assert report["dimensions"]["broadcast_and_confirmation"] == {"total": 8, "passed": 8}
    assert report["dimensions"]["safety"] == {"total": 10, "passed": 10}
    assert {item["id"] for item in report["scenarios"]} == {
        "erc20_swap_without_approval",
        "erc20_swap_with_approval",
        "approval_pending_restart_resume",
        "wallet_rejects_approval",
        "wallet_rejects_swap",
        "swap_temporarily_not_visible",
        "swap_reverted",
        "duplicate_and_conflicting_swap_hash",
        "provider_register_timeout_then_retry",
        "omnibridge_erc20_deposit_order",
    }
    assert all(item["invariants"] for item in report["scenarios"])
    assert "private_key" not in json.dumps(report).lower()
    assert "signedrawtransaction" not in json.dumps(report).lower()


@pytest.mark.asyncio
async def test_single_scenario_report_is_exactly_reproducible():
    report = await run_wallet_app_evals("swap_reverted")
    assert report["summary"]["total"] == 1
    assert [item["id"] for item in report["scenarios"]] == ["swap_reverted"]


def test_exit_codes_distinguish_success_failure_blocked_and_usage_error(capsys):
    assert exit_code({"summary": {"failed": 0, "blocked": 0}}) == 0
    assert exit_code({"summary": {"failed": 1, "blocked": 0}}) == 1
    assert exit_code({"summary": {"failed": 0, "blocked": 1}}) == 1
    assert main(["--scenario", "does-not-exist"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["code"] == "UNKNOWN_SCENARIO"
```

- [ ] **Step 2: Run the report/CLI tests and verify the module is missing**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py -k 'report_has or single_scenario or exit_codes'
```

Expected: collection fails because `evals.wallet_app_evals` does not exist.

- [ ] **Step 3: Implement deterministic aggregation**

Create `src/evals/wallet_app_evals.py`. Run scenario IDs in `SCENARIOS` insertion order. Compute status counts and dimension totals directly from each serialized scenario, never from expected constants:

```python
async def run_wallet_app_evals(scenario_id: str | None = None) -> dict[str, Any]:
    if scenario_id is not None and scenario_id not in SCENARIOS:
        raise UnknownScenarioError(scenario_id)
    ids = [scenario_id] if scenario_id else list(SCENARIOS)
    scenarios = [(await run_scenario(item)).model_dump() for item in ids]
    summary = {
        "total": len(scenarios),
        "passed": sum(item["status"] == "passed" for item in scenarios),
        "failed": sum(item["status"] == "failed" for item in scenarios),
        "blocked": sum(item["status"] == "blocked" for item in scenarios),
    }
    dimensions = {}
    for dimension in ("wallet_api_contract", "broadcast_and_confirmation", "safety"):
        selected = [item for item in scenarios if dimension in item["dimensions"]]
        dimensions[dimension] = {
            "total": len(selected),
            "passed": sum(item["status"] == "passed" for item in selected),
        }
    return sanitize_evidence(
        {
            "schema_version": 1,
            "mode": "offline-wallet-app",
            "summary": summary,
            "dimensions": dimensions,
            "scenarios": scenarios,
        }
    )
```

Populate the `LifecycleReport.dimensions` field defined in Task 1 so scenario-local dimension membership travels with the result.

- [ ] **Step 4: Implement CLI parsing and stable error output**

Use `argparse` with only `--scenario ID`. Do not import `wallet_agent.config.Settings` anywhere in this module. Print exactly one JSON document to stdout:

```python
def exit_code(report: Mapping[str, Any]) -> int:
    summary = report["summary"]
    return 0 if summary["failed"] == 0 and summary["blocked"] == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_wallet_app_evals(args.scenario))
    except UnknownScenarioError as exc:
        print(json.dumps({"code": "UNKNOWN_SCENARIO", "message": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"code": "EVALUATOR_ERROR", "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code(report)
```

- [ ] **Step 5: Verify full-suite and single-scenario CLI output**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_simulator.py tests/evals/test_wallet_app_lifecycle.py
.venv/bin/python -m evals.wallet_app_evals
.venv/bin/python -m evals.wallet_app_evals --scenario approval_pending_restart_resume
```

Expected: tests pass; both commands print parseable JSON; the first summary is `10/10 passed`; the second contains exactly one passed scenario; both exit `0`.

- [ ] **Step 6: Run a deliberate failed-expectation exit-code regression**

Add a test that uses `dataclasses.replace(SCENARIOS["erc20_swap_without_approval"], expected_wallet_sends=2)`, runs that definition through an internal `run_definition` helper, aggregates it, and asserts status `failed` plus `exit_code(...) == 1`. This proves exit code 1 from observable evaluation behavior rather than a handcrafted report only.

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_lifecycle.py -k 'deliberate_failed_expectation or exit_codes'
```

Expected: the test passes and the scenario failure names the wallet send-count mismatch.

- [ ] **Step 7: Commit the report coordinator and CLI**

```bash
git add src/evals/wallet_app_evals.py src/evals/wallet_app_simulator.py tests/evals/test_wallet_app_lifecycle.py
git commit -m "feat: add offline wallet app evaluation cli"
```

---

### Task 9: Document and verify the complete Phase 2 evaluation layer

**Files:**
- Modify: `README.md`
- Modify: `evals/README.md`

**Interfaces:**
- Consumes: Task 8 CLI and all ten scenario IDs.
- Produces: exact operator commands, report/exit-code documentation, explicit offline safety boundary, and a clean full-project verification gate.

- [ ] **Step 1: Add the Phase 2 command to the root verification section**

Add immediately after the Phase 1 command in `README.md`:

```bash
.venv/bin/python -m evals.wallet_app_evals
.venv/bin/python -m evals.wallet_app_evals --scenario approval_pending_restart_resume
```

Explain in one paragraph that `wallet_app_evals` drives the real FastAPI/SSE and compiled LangGraph lifecycle with deterministic wallet/provider/chain simulators, remains offline, and never signs or broadcasts.

- [ ] **Step 2: Document scenario coverage and operational semantics**

Add a `Phase 2 Wallet App lifecycle evaluation` section to `evals/README.md` containing:

- the ten exact scenario IDs;
- full-suite and `--scenario` reproduction commands;
- exit codes 0/1/2;
- report fields `schema_version`, `mode`, `summary`, `dimensions`, `scenarios`, `steps`, `wallet_calls`, `provider_calls`, `invariants`, and `failures`;
- the App-restart definition (client reconstruction only; service/store retained);
- the default offline guarantees and the explicit exclusions for Anvil/real Provider/RPC modes.

- [ ] **Step 3: Run focused evaluator tests**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_app_simulator.py tests/evals/test_wallet_app_lifecycle.py tests/evals/test_wallet_agent_evals.py
```

Expected: all evaluator tests pass.

- [ ] **Step 4: Run the machine-readable evaluation commands**

Run:

```bash
.venv/bin/python -m evals.wallet_agent_evals
.venv/bin/python -m evals.wallet_app_evals
.venv/bin/python -m evals.wallet_app_evals --scenario omnibridge_erc20_deposit_order
```

Expected: Phase 1 remains 15/15; Phase 2 is 10/10; the selected Omni scenario passes; every command exits `0` and prints one parseable JSON document.

- [ ] **Step 5: Run lint and compilation**

Run:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src
```

Expected: Ruff reports `All checks passed!`; compileall exits `0` without output.

- [ ] **Step 6: Run the complete regression suite including browser coverage**

Run:

```bash
.venv/bin/pytest -q
```

Expected: the complete suite passes; browser tests may be skipped only by their existing environment marker, with no new skip or xfail added for Phase 2.

- [ ] **Step 7: Inspect the final diff and safety boundary**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -10
```

Verify the worktree is clean after the planned commits and that the implementation commits contain only the three new evaluator modules, two evaluator test files, the focused API handler/test pair, and the two documentation updates; the already committed spec and plan remain documentation-only commits. Search the generated Phase 2 JSON for credential/signing key names by decoding it in a test—not by grepping source—and confirm no default path imports production settings or network transports.

- [ ] **Step 8: Commit documentation and final verification state**

```bash
git add README.md evals/README.md
git commit -m "docs: document wallet app lifecycle evals"
```

---

## Acceptance Checklist

- [ ] All ten scenario IDs run independently through public REST/SSE endpoints.
- [ ] Approval, swap, and Omni ERC-20 deposit paths use the real compiled graph and application handlers.
- [ ] App restart resumes from public conversation/session IDs without graph/store access.
- [ ] Wallet rejection, RPC invisibility, reverted receipt, provider timeout retry, duplicate hash, and conflicting hash paths are deterministic.
- [ ] Every scenario has bounded continuation/status attempts and a terminal report outcome.
- [ ] All eleven safety invariants pass at 100% for the default suite.
- [ ] Reports are schema version 1, sanitized, JSON-serializable, and reproducible by scenario ID.
- [ ] Default execution reads no `.env`, signs nothing, calls no real provider/RPC, and broadcasts nothing.
- [ ] Phase 1 evals, full pytest, Ruff, compileall, and existing browser tests remain green.
