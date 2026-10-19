"""Narrow structured-output contracts for wallet request understanding."""

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IntentName = Literal[
    "wallet_query",
    "swap_quote",
    "swap_prepare",
    "swap_status",
    "clarification",
    "unsupported",
    "transfer",
    "swap_select",
    "swap_allowance",
    "price_query",
    "transaction_status",
    "market_details",
    "price_history",
    "portfolio_snapshot",
    "portfolio_query",
    "gas_check",
    "asset_discovery",
]

ReadToolName = Literal[
    "wallet_balance",
    "price_lookup",
    "gas_estimate",
    "asset_discovery",
    "transaction_status",
    "market_details",
    "price_history",
    "portfolio_snapshot",
]

_JSON_MODE_TYPES = {"json_object", "response"}
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _strip_json_mode_envelope(value: Any) -> Any:
    """Unwrap DeepSeek json-mode envelopes that wrap the real structured output.

    DeepSeek (with thinking enabled) sometimes returns ``{type: "json_object",
    message|content: <actual JSON>}`` instead of the schema JSON directly. The
    actual draft may be merged at the top level (``message`` as the reply text
    plus ``suggestions``), or nested under ``content``/``data``/``result``.
    Clean dicts are returned untouched.
    """
    if not isinstance(value, Mapping):
        return value
    data = dict(value)
    if str(data.get("type") or "") not in _JSON_MODE_TYPES:
        return data
    data.pop("type", None)
    nested = None
    for key in ("content", "data", "result"):
        if isinstance(data.get(key), Mapping):
            nested = data.pop(key)
            break
    if nested is not None:
        draft = dict(nested)
        if "message" not in draft and isinstance(draft.get("reply"), str):
            draft["message"] = draft.pop("reply")
        # Preserve any sibling top-level keys (e.g. a language hint) while the
        # nested draft's own fields win.
        for key, value in data.items():
            draft.setdefault(key, value)
        return draft
    for key in ("content", "reply"):
        if isinstance(data.get(key), str):
            data["message"] = data[key]
            data.pop(key, None)
            break
    return data


class UnderstandingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def strip_json_mode_marker(cls, value: Any) -> Any:
        data = _strip_json_mode_envelope(value)
        if not isinstance(data, Mapping) or "language" not in cls.model_fields:
            return data
        if not str(data.get("language") or "").strip():
            message = str(data.get("message") or "")
            data = dict(data)
            data["language"] = "zh" if _CJK.search(message) else "en"
        return data


class ResponseSuggestion(UnderstandingModel):
    label: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)


class AgentResponseDraft(UnderstandingModel):
    language: str = Field(min_length=2, max_length=16)
    message: str = Field(min_length=1, max_length=2000)
    suggestions: list[ResponseSuggestion] = Field(default_factory=list, max_length=3)


class ReadToolPlan(UnderstandingModel):
    """A bounded, read-only tool request selected by the fast planner."""

    name: ReadToolName | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class RouteDecision(UnderstandingModel):
    """Task classification only; capability slots use separate schemas."""

    intent: IntentName = Field(description="当前用户请求对应的钱包任务类型")

    @model_validator(mode="before")
    @classmethod
    def discard_json_mode_marker(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and value.get("type") == "json_object":
            normalized = dict(value)
            normalized.pop("type")
            return normalized
        return value


class TransferSlotPatch(UnderstandingModel):
    """Transfer fields explicitly stated in the current user message."""

    chain: str | None = Field(default=None, description="用户指定的转账网络")
    symbol: str | None = Field(default=None, description="用户要转出的资产符号")
    token_address: str | None = Field(default=None, description="用户明确提供的 Token 合约地址")
    decimals: int | None = Field(
        default=None, ge=0, le=255, description="用户明确提供的 Token 精度"
    )
    amount: str | None = Field(default=None, description="人类可读转账数量，不要换算为 raw amount")
    recipient: str | None = Field(default=None, description="用户明确指定的收款地址")


class TransactionStatusSlotPatch(UnderstandingModel):
    """Transaction lookup fields explicitly stated in the current message."""

    transaction_chain: str | None = Field(default=None, description="交易所在网络")
    transaction_hash: str | None = Field(default=None, description="用户提供的交易哈希")


class PriceSlotPatch(UnderstandingModel):
    """Price query asset fields explicitly stated in the current user message."""

    chain: str | None = Field(default=None, description="用户明确指定的资产网络")
    symbol: str | None = Field(default=None, description="用户要查询价格的资产符号")
    token_address: str | None = Field(default=None, description="用户明确提供的 Token 合约地址")
    decimals: int | None = Field(
        default=None, ge=0, le=255, description="用户明确提供的 Token 精度"
    )


class SwapSlotPatch(UnderstandingModel):
    """Swap fields explicitly stated in the current user message."""

    source_chain: str | None = Field(default=None, description="换出资产所在网络")
    destination_chain: str | None = Field(default=None, description="换入资产所在网络")
    source_symbol: str | None = Field(default=None, description="要换出的资产符号")
    destination_symbol: str | None = Field(default=None, description="要换入的资产符号")
    source_token_address: str | None = Field(
        default=None, description="用户明确提供的换出 Token 合约地址"
    )
    destination_token_address: str | None = Field(
        default=None, description="用户明确提供的换入 Token 合约地址"
    )
    input_amount: str | None = Field(
        default=None, description="人类可读换出数量，不要换算为 raw amount"
    )
    output_amount: str | None = Field(
        default=None, description="人类可读换入数量（精确输出），不要换算为 raw amount"
    )
    amount_mode: Literal["exact_in", "exact_out"] | None = Field(
        default=None, description="数量模式：exact_in 表示指定换出，exact_out 表示指定换入"
    )
    target_value_amount: str | None = Field(
        default=None, description="用户明确指定的目标资产美元价值，例如‘价值 10u’中的 10"
    )
    target_value_currency: Literal["USD"] | None = Field(
        default=None, description="目标法币价值的币种，目前仅支持 USD"
    )
    slippage_bps: int | None = Field(
        default=None,
        ge=0,
        le=10_000,
        description="用户设置的滑点，使用基点；1% = 100 bps",
    )

    @field_validator("target_value_amount", mode="before")
    @classmethod
    def normalize_target_value_amount(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return value
