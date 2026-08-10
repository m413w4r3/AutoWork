from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Identity:
    actor_id: str

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")


class IdentityProvider(Protocol):
    async def current(self) -> Identity: ...


class LocalIdentityProvider:
    """Development identity adapter; not a production authentication mechanism."""

    def __init__(self, actor_id: str = "dev-analyst") -> None:
        self._identity = Identity(actor_id=actor_id)

    async def current(self) -> Identity:
        return self._identity
