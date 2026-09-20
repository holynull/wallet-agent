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
        return SseEvent(
            name=name,
            data=decoded if isinstance(decoded, dict) else {"value": decoded},
        )

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
        self.chain_id = hex(int(chain_id, 0))
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
            if outcome.kind == "switch_error":
                raise Eip1193Error(
                    outcome.code or 4902, outcome.message or "chain switch failed"
                )
            self.chain_id = str((params or [])[0]["chainId"]).lower()
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
