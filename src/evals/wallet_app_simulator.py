from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx


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


_FORBIDDEN_EVIDENCE_KEYS = {
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


def _canonical_key(key: Any) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def _normalize_chain_id(chain_id: str) -> str:
    return hex(int(chain_id, 0))


def sanitize_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            canonical_key = _canonical_key(key)
            if canonical_key in _FORBIDDEN_EVIDENCE_KEYS:
                sanitized[key] = "[REDACTED]"
            elif (
                canonical_key in {"data", "calldata"}
                and isinstance(item, str)
                and len(item) > 10
            ):
                sanitized[key] = {"selector": item[:10], "length": len(item)}
            else:
                sanitized[key] = sanitize_evidence(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_evidence(item) for item in value)
    return value


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


async def _response_json(
    response: httpx.Response, operation: str
) -> dict[str, Any]:
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
    if not response.is_success:
        error_body = body if isinstance(body, Mapping) else {}
        raise EvaluationHttpError(
            operation=operation,
            status_code=response.status_code,
            code=str(error_body.get("code", f"HTTP_{response.status_code}")),
            message=str(error_body.get("message", "request failed")),
            details=(
                error_body.get("details")
                if isinstance(error_body.get("details"), Mapping)
                else {}
            ),
        )
    return dict(body)


async def parse_sse(chunks: AsyncIterable[bytes]) -> AsyncIterator[SseEvent]:
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")()

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
        return SseEvent(
            name=name,
            data=decoded if isinstance(decoded, dict) else {"value": decoded},
        )

    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        normalized = buffer.replace("\r\n", "\n")
        while "\n\n" in normalized:
            block, normalized = normalized.split("\n\n", 1)
            event = await decode(block)
            if event is not None:
                yield event
        buffer = normalized
    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        event = await decode(buffer)
        if event is not None:
            yield event


def _public_stage(value: Any) -> str | None:
    if isinstance(value, Mapping):
        stage = value.get("stage")
        if isinstance(stage, str):
            return stage
        for nested in value.values():
            found = _public_stage(nested)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _public_stage(nested)
            if found is not None:
                return found
    return None


class WalletAppClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        user_id: str,
        address: str,
        chain: str,
        conversation_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        turn_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.http = http
        self.user_id = user_id
        self.address = address
        self.chain = chain
        self.conversation_id = conversation_id
        self.session_id = session_id
        self.run_id = run_id
        self.turn_metadata = dict(turn_metadata or {})
        self.stage: str | None = None
        self.steps: list[LifecycleStep] = []
        self.events: list[SseEvent] = []

    @classmethod
    def restore(
        cls,
        http: httpx.AsyncClient,
        *,
        user_id: str,
        address: str,
        chain: str,
        conversation_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> WalletAppClient:
        return cls(
            http,
            user_id=user_id,
            address=address,
            chain=chain,
            conversation_id=conversation_id,
            session_id=session_id,
            run_id=run_id,
        )

    def _record(
        self,
        operation: str,
        *,
        stage_before: str | None,
        status_code: int,
        evidence: Any,
        error_code: str | None = None,
    ) -> None:
        stage_after = _public_stage(evidence) or stage_before
        self.stage = stage_after
        self.steps.append(
            LifecycleStep(
                operation=operation,
                stage_before=stage_before,
                stage_after=stage_after,
                http_status=status_code,
                error_code=error_code,
                evidence=sanitize_evidence(evidence),
            )
        )

    def _record_error(
        self,
        error: EvaluationHttpError,
        *,
        stage_before: str | None,
    ) -> None:
        self._record(
            error.operation,
            stage_before=stage_before,
            status_code=error.status_code,
            evidence=error.details,
            error_code=error.code,
        )

    async def _json(
        self,
        operation: str,
        method: str,
        path: str,
        **request_kwargs: Any,
    ) -> dict[str, Any]:
        stage_before = self.stage
        response = await self.http.request(method, path, **request_kwargs)
        try:
            body = await _response_json(response, operation)
        except EvaluationHttpError as exc:
            self._record_error(exc, stage_before=stage_before)
            raise
        self._record(
            operation,
            stage_before=stage_before,
            status_code=response.status_code,
            evidence=body,
        )
        return body

    def _require_session(self) -> str:
        if self.session_id is None:
            raise RuntimeError("session_id is required for this operation")
        return self.session_id

    async def turn(self, message: str) -> list[SseEvent]:
        metadata = dict(self.turn_metadata)
        turn_stage_before = self.stage
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
        try:
            body = await _response_json(response, "turn")
        except EvaluationHttpError as exc:
            self._record_error(exc, stage_before=turn_stage_before)
            raise
        self.turn_metadata.clear()
        self.run_id = str(body["run_id"])
        self.conversation_id = str(body["conversation_id"])
        if body.get("session_id"):
            self.session_id = str(body["session_id"])
        self._record(
            "turn",
            stage_before=turn_stage_before,
            status_code=response.status_code,
            evidence=body,
        )

        stream_stage_before = self.stage
        async with self.http.stream(
            "GET", f"/v1/agent/stream/{self.run_id}"
        ) as stream:
            if not stream.is_success:
                await stream.aread()
                try:
                    await _response_json(stream, "stream")
                except EvaluationHttpError as exc:
                    self._record_error(exc, stage_before=stream_stage_before)
                    raise
            events = [event async for event in parse_sse(stream.aiter_bytes())]
            stream_status = stream.status_code
        self.events.extend(events)
        self._record(
            "stream",
            stage_before=stream_stage_before,
            status_code=stream_status,
            evidence={
                "events": [
                    {"name": event.name, "data": event.data} for event in events
                ]
            },
        )
        return events

    async def session(self) -> dict[str, Any]:
        return await self._json(
            "session",
            "GET",
            f"/v1/swap/{self._require_session()}",
            params={"user_id": self.user_id},
        )

    async def select_quote(self, provider_reference: str) -> dict[str, Any]:
        return await self._json(
            "select_quote",
            "POST",
            f"/v1/swap/{self._require_session()}/select-quote",
            json={
                "user_id": self.user_id,
                "provider_reference": provider_reference,
            },
        )

    async def confirm(self, approved: bool) -> dict[str, Any]:
        return await self._json(
            "confirm",
            "POST",
            f"/v1/swap/{self._require_session()}/confirm",
            json={"user_id": self.user_id, "approved": approved},
        )

    async def submit_approval_hash(
        self, chain: str, tx_hash: str
    ) -> dict[str, Any]:
        return await self._json(
            "approve_broadcast",
            "POST",
            f"/v1/swap/{self._require_session()}/approve-broadcast",
            json={
                "user_id": self.user_id,
                "chain": chain,
                "approve_tx_hash": tx_hash,
            },
        )

    async def continue_swap(self) -> dict[str, Any]:
        return await self._json(
            "continue",
            "POST",
            f"/v1/swap/{self._require_session()}/continue",
            json={"user_id": self.user_id},
        )

    async def submit_swap_hash(
        self, chain: str, tx_hash: str
    ) -> dict[str, Any]:
        return await self._json(
            "broadcast",
            "POST",
            f"/v1/swap/{self._require_session()}/broadcast",
            json={"user_id": self.user_id, "chain": chain, "tx_hash": tx_hash},
        )

    async def transaction_status(
        self, chain: str, tx_hash: str
    ) -> dict[str, Any]:
        return await self._json(
            "transaction_status",
            "GET",
            f"/v1/transactions/{chain}/{tx_hash}",
            params={"user_id": self.user_id},
        )


class Eip1193Error(RuntimeError):
    def __init__(self, code: int | str, message: str) -> None:
        self.code = code
        super().__init__(message)


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
        self.chain_id = _normalize_chain_id(chain_id)
        self.accounts = tuple(accounts)
        self.switch_outcomes = switch_outcomes or FaultSequence(
            fallback=FaultOutcome("success")
        )
        self.send_outcomes = send_outcomes or FaultSequence(
            fallback=FaultOutcome(
                "rpc_error", message="wallet send outcome not configured"
            )
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
            if outcome.kind != "success":
                default_code = (
                    4902
                    if outcome.kind == "switch_error"
                    else 4001
                    if outcome.kind == "reject"
                    else -32603
                )
                raise Eip1193Error(
                    outcome.code if outcome.code is not None else default_code,
                    outcome.message
                    if outcome.message is not None
                    else "chain switch failed",
                )
            self.chain_id = _normalize_chain_id(str((params or [])[0]["chainId"]))
            return None
        if method == "wallet_addEthereumChain":
            self._record(method, params or [])
            return None
        if method != "eth_sendTransaction":
            self._record(method, params or [])
            raise Eip1193Error(4200, f"Unsupported wallet method: {method}")
        transaction = dict((params or [])[0])
        forbidden = {
            "privatekey",
            "signature",
            "raw",
            "rawtransaction",
            "signedrawtransaction",
        }
        if any(
            "".join(character for character in key.lower() if character.isalnum())
            in forbidden
            for key in transaction
        ):
            redacted_transaction = {
                key: "[REDACTED]" if _canonical_key(key) in forbidden else value
                for key, value in transaction.items()
            }
            self._record(method, [redacted_transaction])
            raise ValueError("signed transaction material is forbidden")
        self._record(method, [transaction])
        outcome = self.send_outcomes.next()
        if outcome.kind == "success":
            return str(outcome.value)
        if outcome.kind == "account_change":
            self.accounts = (str(outcome.value),)
            raise Eip1193Error(4100, "wallet account changed before signing")
        if outcome.kind == "chain_change":
            self.chain_id = _normalize_chain_id(str(outcome.value))
            raise Eip1193Error(4901, "wallet chain changed before signing")
        raise Eip1193Error(outcome.code or 4001, outcome.message or outcome.kind)
