"""Server-side model registry and request-level router."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from .contracts import (
    AgentResponseDraft,
    PriceSlotPatch,
    ReadToolPlan,
    RouteDecision,
    SwapSlotPatch,
    TransactionStatusSlotPatch,
    TransferSlotPatch,
)


def _instruction_messages(instructions: str, payload_label: str, payload: str) -> list[Any]:
    """Keep stable role instructions separate from dynamic data for prefix caching."""
    return [
        SystemMessage(content=instructions),
        HumanMessage(content=f"{payload_label}\n{payload}"),
    ]


def _model_input(request: Mapping[str, Any]) -> list[Any]:
    """Adapt the agent's serializable request state to LangChain input."""
    payload = json.dumps(dict(request), ensure_ascii=False, default=str)
    instructions = (
        "你是 Wallet Agent 的意图识别器。请根据下面的请求判断用户意图，"
        "并只返回一个合法 JSON 对象，使用结构化输出中允许的字段。"
        "不要输出 Markdown、解释文字或代码块。可选 intent 包括："
        "wallet_query、swap_quote、swap_prepare、swap_status、"
        "swap_select、swap_allowance、transfer、price_query、transaction_status、portfolio_query、"
        "gas_check、asset_discovery、market_details、price_history、"
        "clarification、unsupported。"
        "如果用户只是问候、与钱包无关，或完全无法识别任务，才使用 clarification。"
        "当前用户消息优先于 conversation_history、previous_read_response 和 previous_preflight；"
        "历史只用于理解代词、未完成任务和上下文，不能覆盖当前明确的新请求。"
        "‘我的全部余额’、‘所有资产有多少’、‘全钱包余额’表示 wallet_query 或 portfolio_query，"
        "不要因为历史里有交易预检查失败就改成 clarification。"
        "只有用户明确确认‘是不是余额不足’、‘是否因为余额不够’时，"
        "才把 previous_preflight 当作追问依据。"
        "参数不完整的转账仍然是 transfer，参数不完整的兑换仍然是 swap_quote。"
        "兑换请求即使只写了类似‘帮我兑换 1USDT’的紧凑格式，也要识别为 swap_quote。"
        "不要提取参数；不要因为缺少链、Token、金额、地址或钱包连接而改变任务类型。"
    )
    return _instruction_messages(
        instructions,
        "请求 JSON（conversation_history 包含当前 thread 的完整公开对话历史）：",
        payload,
    )


def _slot_model_input(task_kind: str, request: Mapping[str, Any]) -> list[HumanMessage]:
    payload = json.dumps(dict(request), ensure_ascii=False, default=str)
    if task_kind == "price":
        instructions = (
            "你是 Wallet Agent 的 price 参数提取器。只返回合法 JSON，允许且必须仅使用这些 key："
            "chain、symbol、token_address、decimals。只提取本轮用户明确提供的资产信息；"
            "未提供的值返回 null，不要猜测网络、Token 地址或精度。中文‘比特币’写 symbol=BTC，"
            "‘以太坊’写 symbol=ETH；‘BTC 的价格’和‘ETH price’都必须提取 symbol。"
        )
    elif task_kind == "transfer":
        instructions = (
            "你是 Wallet Agent 的 transfer 参数提取器。参数不完整时仍然是 transfer。"
            "只返回合法 JSON，允许且必须仅使用这些 key：chain、symbol、token_address、"
            "decimals、amount、recipient。只提取本轮用户明确提供或明确修改的值；"
            "未提供的值返回 null，不要重复旧值。amount 保留人类可读字符串，不要计算 raw amount。"
            "如果 request.suggestion_data 存在，它是用户点击建议明确确认的槽位输入，"
            "优先使用其中的值，仍须遵守本规则。"
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
            "destination_token_address、input_amount、output_amount、amount_mode、"
            "target_value_amount、target_value_currency、slippage_bps。"
            "只提取本轮用户明确提供或明确修改的值；"
            "未提供的值返回 null，不要重复旧值。input_amount/output_amount 保留人类可读字符串。"
            "如果 request.suggestion_data 存在，它是用户点击建议明确确认的槽位输入，"
            "优先使用其中的值，仍须遵守本规则。"
            "滑点使用基点整数：1% 写 100，0.5% 写 50，25 bps 写 25。"
            "不要猜测 Token 地址、精度、金额或链。"
            "中文里‘换一些 USDT’、‘兑换一点 USDT’表示目标资产是 USDT，必须写入"
            "destination_symbol，不是 source_symbol。‘用 USDC 换’表示来源资产是 USDC；"
            "如果已有任务中保存了目标资产，不要覆盖或反转它。"
            "‘价值 10u’、‘价值 10 USD’或‘worth 10 USD’表达的是目标资产美元价值，"
            "不是来源或目标 Token 数量；此时 target_value_amount 填数字、"
            "target_value_currency 填 USD，input_amount 和 output_amount 都返回 null。"
            "示例：‘在 Base 用 1 USDC 换 USDT’对应 source_chain=Base、"
            "destination_chain=Base、source_symbol=USDC、destination_symbol=USDT、"
            "input_amount=1。示例：‘都在 Base 链’只输出两个 chain 字段；"
            "如果用户说‘换到 5 USDT’或‘想要 5 USDT’，这是 exact_out，输出 output_amount=5、"
            "amount_mode=exact_out，不要把 5 写入 input_amount。"
            "指定来源数量时输出 amount_mode=exact_in。"
            "‘来源也在 Base 链’只输出 source_chain=Base；‘目标也在 Base 链’只输出"
            "destination_chain=Base，其余字段为 null。"
        )
    elif task_kind == "transaction_status":
        instructions = (
            "你是 Wallet Agent 的 transaction_status 参数提取器。"
            "只返回合法 JSON，允许且必须仅使用这些 key：transaction_chain、"
            "transaction_hash。只提取本轮用户明确提供的值；未提供的值返回 null，"
            "不要重复对话历史中的旧值，也不要猜测网络或交易哈希。"
            "示例：‘以太，0xabc...’应分别提取 transaction_chain=ETH 和完整的 "
            "transaction_hash；只发送哈希时 transaction_chain 必须为 null。"
        )
    else:
        raise ValueError(f"unsupported task extractor: {task_kind}")
    return _instruction_messages(
        instructions,
        "请求、完整对话历史和已有任务 JSON：",
        payload,
    )


def _response_model_input(
    request: Mapping[str, Any],
    facts: Mapping[str, Any],
    messages: list[Any] | None = None,
) -> list[Any]:
    payload = json.dumps(
        {"request": dict(request), "facts": dict(facts)}, ensure_ascii=False, default=str
    )
    instructions = (
        "你是 Wallet Agent 的用户回复生成器。根据用户本轮语言与给定 facts，生成简洁、自然的回复；"
        "用户用英文就全程英文，用户用中文就全程中文，不要中英混排。"
        "不要输出 Markdown、代码块或解释模型自身。"
        "你只能复述 facts 中明确提供的余额、支持链、资产和价格，不能编造或推断事实。"
        "需要补充信息时，提出自然的问题，并可提供最多 3 个建议。"
        "每条建议必须包含 label、message、data；"
        "data 只能填写 facts 或已确认任务中可核验的结构化槽位。"
        "建议只是供用户选择的输入，不代表同意执行。"
        "若建议为兑换，先从 balances 中选择有余额的来源资产，并从 supported_assets 中选择目标资产；"
        "只选择 supported_chains 中的链。"
        "数量不得超过对应余额；有价格时建议考虑 USD 价值并在措辞中保持准确。"
        "绝不调用工具、不执行查询、签名、授权、广播或转账/兑换；不要生成交易数据、私钥或签名内容。"
        "仅返回符合结构化输出 schema 的 JSON。"
    )
    if not messages:
        return _instruction_messages(instructions, "request 和 facts JSON：", payload)
    transcript = list(messages)
    if transcript and isinstance(transcript[0], SystemMessage):
        transcript = transcript[1:]
    return [
        SystemMessage(content=instructions),
        *transcript,
        HumanMessage(
            content=(
                "请基于上面的原始问题、工具调用和 ToolMessage，以及下面经过服务端校验的 "
                f"request/facts JSON 生成最终回复：\n{payload}"
            )
        ),
    ]


def _read_model_input(request: Mapping[str, Any]) -> list[HumanMessage]:
    payload = json.dumps(dict(request), ensure_ascii=False, default=str)
    instructions = (
        "你是 Wallet Agent 的快速只读查询规划器。只能选择一个只读工具，或者返回 name=null。"
        "允许的工具只有 wallet_balance、price_lookup、gas_estimate、asset_discovery、"
        "transaction_status、market_details、price_history、portfolio_snapshot。"
        "不要执行转账、兑换、授权、签名或广播。"
        "已有 read_facts 足以回答用户时返回 name=null；需要补充事实时才选择工具。"
        "这是一个有状态的多步计划：先查看 task_plan 和 read_facts，"
        "只选择下一步尚未完成且能减少不确定性的工具；"
        "如果缺少用户必须确认的网络、钱包地址或资产选择，返回 name=null，让上层向用户提问。"
        "arguments 只能填写用户请求、对话引用或已验证事实中明确可用的参数。"
        "只返回结构化 JSON。不要输出 reasoning、Markdown 或额外字段。"
    )
    return _instruction_messages(instructions, "request JSON：", payload)


class ModelRegistry:
    def __init__(
        self,
        models: Mapping[str, Any],
        *,
        default_model_id: str,
        classifier_model_id: str | None = None,
        extractor_model_id: str | None = None,
        response_model_id: str | None = None,
        read_agent_model_id: str | None = None,
        extractors: Mapping[str, Mapping[str, Any]] | None = None,
        responses: Mapping[str, Any] | None = None,
        readers: Mapping[str, Any] | None = None,
        raw_models: Mapping[str, Any] | None = None,
    ) -> None:
        if default_model_id not in models:
            raise ValueError(f"default model is not configured: {default_model_id}")
        self._models = dict(models)
        self._extractors = {
            str(kind): dict(kind_models) for kind, kind_models in (extractors or {}).items()
        }
        self._responses = dict(responses or {})
        self._readers = dict(readers or {})
        self._raw_models = dict(raw_models or models)
        self.default_model_id = default_model_id
        self.classifier_model_id = classifier_model_id or default_model_id
        self.extractor_model_id = extractor_model_id or default_model_id
        self.response_model_id = response_model_id or default_model_id
        self.read_agent_model_id = read_agent_model_id or default_model_id
        for role, model_id in (
            ("classifier", self.classifier_model_id),
            ("extractor", self.extractor_model_id),
            ("response", self.response_model_id),
            ("read agent", self.read_agent_model_id),
        ):
            if model_id not in self._models:
                raise ValueError(f"{role} model is not configured: {model_id}")
        if self._readers and self.read_agent_model_id not in self._readers:
            raise ValueError(
                f"read agent model is not configured: {self.read_agent_model_id}"
            )

    @property
    def read_agent_enabled(self) -> bool:
        return self.read_agent_model_id in self._readers

    def get_read_model(self, model_id: str | None = None) -> Any:
        selected = model_id or self.read_agent_model_id
        try:
            return self._readers[selected]
        except KeyError as exc:
            raise ValueError(f"read agent is not configured: model={selected}") from exc

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(self._models)

    @property
    def role_model_ids(self) -> dict[str, str]:
        return {
            "classifier": self.classifier_model_id,
            "extractor": self.extractor_model_id,
            "response": self.response_model_id,
            "read_agent": self.read_agent_model_id,
        }

    def get(self, model_id: str | None = None) -> Any:
        selected = model_id or self.default_model_id
        try:
            return self._models[selected]
        except KeyError as exc:
            raise ValueError(f"unknown model_id: {selected}") from exc

    def get_raw(self, model_id: str | None = None) -> Any:
        selected = model_id or self.read_agent_model_id
        try:
            return self._raw_models[selected]
        except KeyError as exc:
            raise ValueError(f"raw model is not configured: {selected}") from exc

    def get_extractor(self, task_kind: str, model_id: str | None = None) -> Any:
        selected = model_id or self.extractor_model_id
        try:
            return self._extractors[task_kind][selected]
        except KeyError as exc:
            raise ValueError(
                f"extractor is not configured: task={task_kind}, model={selected}"
            ) from exc

    def get_response_model(self, model_id: str | None = None) -> Any:
        selected = model_id or self.response_model_id
        try:
            return self._responses[selected]
        except KeyError as exc:
            raise ValueError(f"response model is not configured: model={selected}") from exc

    def validate(self, model_id: str | None = None) -> str:
        selected = model_id or self.default_model_id
        if selected not in self._models:
            raise ValueError(f"unknown model_id: {selected}")
        return selected


class ModelRouter:
    """LangGraph-compatible model facade selecting a registry model per request."""

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry
        self.read_agent_enabled = registry.read_agent_enabled
        self.metrics: Any = None
        self._model_trace: list[dict[str, Any]] = []
        self._model_call_index = 0

    @property
    def supports_react(self) -> bool:
        """Whether the configured default raw model exposes native tool binding."""
        try:
            model = self.registry.get_raw(self.registry.default_model_id)
        except ValueError:
            return False
        return callable(getattr(model, "bind_tools", None))

    def _record_model(
        self,
        role: str,
        model_id: str,
        *,
        elapsed_ms: int,
        stage: str,
        **details: Any,
    ) -> None:
        self._model_call_index += 1
        entry = {
            "call_id": f"{role}-{self._model_call_index}",
            "role": role,
            "model_id": model_id,
            "stage": stage,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "elapsed_ms": elapsed_ms,
            **details,
        }
        self._model_trace.append(entry)

    @staticmethod
    def _response_usage(value: Any) -> dict[str, Any]:
        """Normalize OpenAI-compatible usage metadata without persisting reasoning text."""
        response_metadata = getattr(value, "response_metadata", None) or {}
        usage_metadata = getattr(value, "usage_metadata", None) or {}
        token_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}

        def first(*values: Any) -> Any:
            return next((item for item in values if item is not None), None)

        prompt_tokens = first(
            usage_metadata.get("input_tokens"),
            token_usage.get("prompt_tokens"),
            response_metadata.get("prompt_tokens"),
        )
        completion_tokens = first(
            usage_metadata.get("output_tokens"),
            token_usage.get("completion_tokens"),
            response_metadata.get("completion_tokens"),
        )
        total_tokens = first(
            usage_metadata.get("total_tokens"),
            token_usage.get("total_tokens"),
            response_metadata.get("total_tokens"),
        )
        prompt_details = token_usage.get("prompt_tokens_details") or {}
        cached_tokens = first(
            prompt_details.get("cached_tokens"),
            token_usage.get("cached_tokens"),
            response_metadata.get("prompt_cache_hit_tokens"),
        )
        finish_reason = first(
            response_metadata.get("finish_reason"),
            response_metadata.get("finish"),
        )
        details: dict[str, Any] = {}
        for key, item in (
            ("prompt_tokens", prompt_tokens),
            ("completion_tokens", completion_tokens),
            ("total_tokens", total_tokens),
            ("cached_tokens", cached_tokens),
            ("finish_reason", finish_reason),
        ):
            if item is not None:
                details[key] = item
        return details

    async def _invoke(
        self,
        *,
        role: str,
        model_id: str,
        stage: str,
        model: Any,
        request: Any,
        **details: str,
    ) -> Any:
        started = perf_counter()
        result: Any = None
        status = "success"
        error_code: str | None = None
        try:
            if hasattr(model, "ainvoke"):
                result = await model.ainvoke(request)
            else:
                result = model.invoke(request)
            return result
        except Exception:
            status = "error"
            error_code = "MODEL_INVOCATION_FAILED"
            raise
        finally:
            elapsed_ms = max(0, round((perf_counter() - started) * 1000))
            self._record_model(
                role,
                model_id,
                elapsed_ms=elapsed_ms,
                stage=stage,
                **self._response_usage(result),
                **details,
            )
            if self.metrics is not None:
                self.metrics.record_model(
                    model_id,
                    role=role,
                    elapsed_ms=elapsed_ms,
                    status=status,
                    error_code=error_code,
                )

    def consume_model_trace(self) -> list[dict[str, Any]]:
        trace = list(self._model_trace)
        self._model_trace.clear()
        return trace

    async def plan_read(self, request: Mapping[str, Any]) -> ReadToolPlan:
        model_id = str(request.get("read_agent_model_id") or self.registry.read_agent_model_id)
        model = self.registry.get_read_model(model_id)
        model_request = _read_model_input(request)
        result = await self._invoke(
            role="read_agent", model_id=model_id, stage="查询规划",
            model=model, request=model_request,
        )
        return result if isinstance(result, ReadToolPlan) else ReadToolPlan.model_validate(result)

    async def invoke_read_tools(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]],
        model_id: str | None = None,
    ) -> Any:
        """Run the native tool-calling read model and preserve its call IDs."""
        selected = str(model_id or self.registry.read_agent_model_id)
        raw_model = self.registry.get_raw(selected)
        binder = getattr(raw_model, "bind_tools", None)
        if not callable(binder):
            raise ValueError(f"read model does not support tool calls: {selected}")
        try:
            model = binder(
                tools,
                tool_choice="auto",
                parallel_tool_calls=True,
            )
        except TypeError:
            # Keep injected legacy model doubles and older OpenAI-compatible
            # clients usable while the production client supports both flags.
            model = binder(tools)
        result = await self._invoke(
            role="read_agent",
            model_id=selected,
            stage="只读工具调用",
            model=model,
            request=messages,
        )
        calls = getattr(result, "tool_calls", None) or []
        if self._model_trace:
            self._model_trace[-1]["tool_call_ids"] = [
                str(item.get("id"))
                for item in calls
                if isinstance(item, Mapping) and item.get("id")
            ]
        return result

    async def invoke_react(
        self,
        messages: list[Any],
        tools: list[Any],
        model_id: str | None = None,
    ) -> AIMessage:
        """Invoke the single conversational model bound to all wallet tools."""
        selected = str(model_id or self.registry.default_model_id)
        raw_model = self.registry.get_raw(selected)
        binder = getattr(raw_model, "bind_tools", None)
        if not callable(binder):
            raise ValueError(f"react model does not support tool calls: {selected}")
        try:
            bound = binder(tools, tool_choice="auto", parallel_tool_calls=True)
        except TypeError:
            bound = binder(tools)
        result = await self._invoke(
            role="react_agent",
            model_id=selected,
            stage="ReAct 工具决策",
            model=bound,
            request=messages,
            tool_count=str(len(tools)),
        )
        calls = getattr(result, "tool_calls", None) or []
        if self._model_trace:
            self._model_trace[-1]["tool_call_ids"] = [
                str(item.get("id"))
                for item in calls
                if isinstance(item, Mapping) and item.get("id")
            ]
        if isinstance(result, AIMessage):
            return result
        if isinstance(result, Mapping):
            return AIMessage(
                content=str(result.get("content") or result.get("message") or ""),
                tool_calls=list(result.get("tool_calls") or []),
            )
        return AIMessage(content=str(result or ""))

    async def classify(self, request: Mapping[str, Any]) -> RouteDecision:
        model_id = str(request.get("model_id") or self.registry.classifier_model_id)
        model = self.registry.get(model_id)
        model_request = _model_input(request) if isinstance(request, Mapping) else request
        result = await self._invoke(
            role="classifier", model_id=model_id, stage="请求识别",
            model=model, request=model_request,
        )
        return result if isinstance(result, RouteDecision) else RouteDecision.model_validate(result)

    async def extract(
        self, task_kind: str, request: Mapping[str, Any]
    ) -> TransferSlotPatch | SwapSlotPatch | TransactionStatusSlotPatch:
        model_id = str(request.get("model_id") or self.registry.extractor_model_id)
        model = self.registry.get_extractor(task_kind, model_id)
        model_request = _slot_model_input(task_kind, request)
        stage_names = {
            "price": "价格参数提取",
            "transfer": "转账参数提取",
            "swap": "兑换参数提取",
            "transaction_status": "交易状态参数提取",
        }
        result = await self._invoke(
            role="extractor", model_id=model_id,
            stage=stage_names.get(task_kind, "请求参数提取"),
            model=model, request=model_request, task_kind=task_kind,
        )
        contracts = {
            "price": PriceSlotPatch,
            "transfer": TransferSlotPatch,
            "swap": SwapSlotPatch,
            "transaction_status": TransactionStatusSlotPatch,
        }
        try:
            contract = contracts[task_kind]
        except KeyError as exc:
            raise ValueError(f"unsupported task extractor: {task_kind}") from exc
        return result if isinstance(result, contract) else contract.model_validate(result)

    async def respond(
        self,
        request: Mapping[str, Any],
        facts: Mapping[str, Any],
        messages: list[Any] | None = None,
    ) -> AgentResponseDraft:
        model_id = str(request.get("model_id") or self.registry.response_model_id)
        model = self.registry.get_response_model(model_id)
        model_request = _response_model_input(request, facts, messages)
        result = await self._invoke(
            role="response", model_id=model_id, stage="生成回复",
            model=model, request=model_request,
        )
        return (
            result
            if isinstance(result, AgentResponseDraft)
            else AgentResponseDraft.model_validate(result)
        )

    async def ainvoke(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Compatibility facade for graph callers that only classify requests."""
        return (await self.classify(request)).model_dump(mode="json")
