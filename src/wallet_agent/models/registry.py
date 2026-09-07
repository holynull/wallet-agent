"""Server-side model registry and request-level router."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
        if hasattr(model, "ainvoke"):
            return await model.ainvoke(request)
        return model.invoke(request)
