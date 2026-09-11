"""Server-side model registry and request-level router."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage


def _model_input(request: Mapping[str, Any]) -> list[HumanMessage]:
    """Adapt the agent's serializable request state to LangChain input."""
    payload = json.dumps(dict(request), ensure_ascii=False, default=str)
    return [
        HumanMessage(
            content=(
                "你是 Wallet Agent 的意图识别器。请根据下面的请求判断用户意图，"
                "并只返回一个合法 JSON 对象，使用结构化输出中允许的字段。"
                "不要输出 Markdown、解释文字或代码块。可选 intent 包括："
                "wallet_query、swap_quote、swap_prepare、swap_status、"
                "transfer、price_query、transaction_status、portfolio_query、"
                "gas_check、asset_discovery、clarification、unsupported。"
                "如果用户只是问候或信息不足，请使用 clarification。"
                "兑换请求即使只写了类似“帮我兑换 1USDT”的紧凑格式，也要识别为 swap_quote；"
                "尽可能提取金额和 Token，其余信息留给系统向用户追问。"
                "不要因为缺少链、来源 Token 或钱包连接而改成 unsupported。\n\n"
                f"请求 JSON：\n{payload}"
            )
        )
    ]


class ModelRegistry:
    def __init__(self, models: Mapping[str, Any], *, default_model_id: str) -> None:
        if default_model_id not in models:
            raise ValueError(f"default model is not configured: {default_model_id}")
        self._models = dict(models)
        self.default_model_id = default_model_id

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(self._models)

    def get(self, model_id: str | None = None) -> Any:
        selected = model_id or self.default_model_id
        try:
            return self._models[selected]
        except KeyError as exc:
            raise ValueError(f"unknown model_id: {selected}") from exc

    def validate(self, model_id: str | None = None) -> str:
        selected = model_id or self.default_model_id
        if selected not in self._models:
            raise ValueError(f"unknown model_id: {selected}")
        return selected


class ModelRouter:
    """LangGraph-compatible model facade selecting a registry model per request."""

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    async def ainvoke(self, request: Mapping[str, Any]) -> Any:
        model = self.registry.get(request.get("model_id"))
        model_request = _model_input(request) if isinstance(request, Mapping) else request
        if hasattr(model, "ainvoke"):
            return await model.ainvoke(model_request)
        return model.invoke(model_request)
