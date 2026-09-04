"""Structured errors that can cross API and graph boundaries safely."""

from enum import Enum

from .models import AgentError


class ChainCapabilityUnavailable(Exception):
    """Raised when an adapter explicitly does not implement an operation."""

    code = "CHAIN_CAPABILITY_UNAVAILABLE"

    def __init__(self, chain: str, capability: str | Enum):
        self.chain = chain
        self.capability = capability.value if isinstance(capability, Enum) else capability
        self.message = f"Chain {chain} does not support {self.capability}."
        super().__init__(self.message)

    def to_agent_error(self) -> AgentError:
        return AgentError(
            code=self.code,
            message=self.message,
            details={"chain": self.chain, "capability": self.capability},
        )
