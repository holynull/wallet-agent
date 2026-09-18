"""Narrow structured-output contracts for wallet request understanding."""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "portfolio_query",
    "gas_check",
    "asset_discovery",
]


class UnderstandingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    token_address: str | None = Field(
        default=None, description="用户明确提供的 Token 合约地址"
    )
    decimals: int | None = Field(
        default=None, ge=0, le=255, description="用户明确提供的 Token 精度"
    )
    amount: str | None = Field(
        default=None, description="人类可读转账数量，不要换算为 raw amount"
    )
    recipient: str | None = Field(default=None, description="用户明确指定的收款地址")


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
