"""Server-side model registry and request-level router."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage

from .contracts import RouteDecision, SwapSlotPatch, TransferSlotPatch


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
                "如果用户只是问候、与钱包无关，或完全无法识别任务，才使用 clarification。"
                "参数不完整的转账仍然是 transfer，参数不完整的兑换仍然是 swap_quote。"
                "兑换请求即使只写了类似“帮我兑换 1USDT”的紧凑格式，也要识别为 swap_quote。"
                "不要提取参数；不要因为缺少链、Token、金额、地址或钱包连接而改变任务类型。\n\n"
                f"请求 JSON：\n{payload}"
            )
        )
    ]


def _slot_model_input(task_kind: str, request: Mapping[str, Any]) -> list[HumanMessage]:
    payload = json.dumps(dict(request), ensure_ascii=False, default=str)
    if task_kind == "transfer":
        instructions = (
            "你是 Wallet Agent 的 transfer 参数提取器。参数不完整时仍然是 transfer。"
            "只返回合法 JSON，允许且必须仅使用这些 key：chain、symbol、token_address、"
            "decimals、amount、recipient。只提取本轮用户明确提供或明确修改的值；"
            "未提供的值返回 null，不要重复旧值。amount 保留人类可读字符串，不要计算 raw amount。"
            "不要猜测 Token 地址、精度、金额、链或收款地址。"
            "示例：‘在 Base 给 0x2222...2222 转 0.01 ETH’对应 "
            '{"chain":"Base","symbol":"ETH","token_address":null,'
            '"decimals":null,"amount":"0.01","recipient":"0x2222...2222"}。'
            "示例：‘改成 2 USDC’只输出 symbol=USDC、amount=2，其余字段为 null。"
        )
    elif task_kind == "swap":
        instructions = (
            "你是 Wallet Agent 的 swap 参数提取器。参数不完整时仍然是 swap。"
            "只返回合法 JSON，允许且必须仅使用这些 key：source_chain、destination_chain、"
            "source_symbol、destination_symbol、source_token_address、"
            "destination_token_address、input_amount、output_amount、amount_mode。只提取本轮用户明确提供或明确修改的值；"
            "未提供的值返回 null，不要重复旧值。input_amount/output_amount 保留人类可读字符串。"
            "不要猜测 Token 地址、精度、金额或链。"
            "中文里‘换一些 USDT’、‘兑换一点 USDT’表示目标资产是 USDT，必须写入"
            "destination_symbol，不是 source_symbol。‘用 USDC 换’表示来源资产是 USDC；"
            "如果已有任务中保存了目标资产，不要覆盖或反转它。"
            "示例：‘在 Base 用 1 USDC 换 USDT’对应 source_chain=Base、"
            "destination_chain=Base、source_symbol=USDC、destination_symbol=USDT、"
            "input_amount=1。示例：‘都在 Base 链’只输出两个 chain 字段；"
            "如果用户说‘换到 5 USDT’或‘想要 5 USDT’，这是 exact_out，输出 output_amount=5、"
            "amount_mode=exact_out，不要把 5 写入 input_amount。"
            "指定来源数量时输出 amount_mode=exact_in。"
            "‘来源也在 Base 链’只输出 source_chain=Base；‘目标也在 Base 链’只输出"
            "destination_chain=Base，其余字段为 null。"
        )
    else:
        raise ValueError(f"unsupported task extractor: {task_kind}")
    return [HumanMessage(content=f"{instructions}\n\n请求和已有任务 JSON：\n{payload}")]


class ModelRegistry:
    def __init__(
        self,
        models: Mapping[str, Any],
        *,
        default_model_id: str,
        extractors: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if default_model_id not in models:
            raise ValueError(f"default model is not configured: {default_model_id}")
        self._models = dict(models)
        self._extractors = {
            str(kind): dict(kind_models) for kind, kind_models in (extractors or {}).items()
        }
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

    def get_extractor(self, task_kind: str, model_id: str | None = None) -> Any:
        selected = model_id or self.default_model_id
        try:
            return self._extractors[task_kind][selected]
        except KeyError as exc:
            raise ValueError(
                f"extractor is not configured: task={task_kind}, model={selected}"
            ) from exc

    def validate(self, model_id: str | None = None) -> str:
        selected = model_id or self.default_model_id
        if selected not in self._models:
            raise ValueError(f"unknown model_id: {selected}")
        return selected


class ModelRouter:
    """LangGraph-compatible model facade selecting a registry model per request."""

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    async def classify(self, request: Mapping[str, Any]) -> RouteDecision:
        model = self.registry.get(request.get("model_id"))
        model_request = _model_input(request) if isinstance(request, Mapping) else request
        if hasattr(model, "ainvoke"):
            result = await model.ainvoke(model_request)
        else:
            result = model.invoke(model_request)
        return result if isinstance(result, RouteDecision) else RouteDecision.model_validate(result)

    async def extract(
        self, task_kind: str, request: Mapping[str, Any]
    ) -> TransferSlotPatch | SwapSlotPatch:
        model = self.registry.get_extractor(task_kind, request.get("model_id"))
        model_request = _slot_model_input(task_kind, request)
        if hasattr(model, "ainvoke"):
            result = await model.ainvoke(model_request)
        else:
            result = model.invoke(model_request)
        contract = TransferSlotPatch if task_kind == "transfer" else SwapSlotPatch
        return result if isinstance(result, contract) else contract.model_validate(result)

    async def ainvoke(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Compatibility facade for graph callers that only classify requests."""
        return (await self.classify(request)).model_dump(mode="json")
