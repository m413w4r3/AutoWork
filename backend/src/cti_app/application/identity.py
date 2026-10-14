from dataclasses import dataclass
from hmac import compare_digest
from ipaddress import ip_address
from typing import Protocol

PROTECTED_APP_ENVIRONMENTS = frozenset({"pilot", "prod", "production", "live"})


@dataclass(frozen=True, slots=True)
class Identity:
    actor_id: str

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")


class IdentityProvider(Protocol):
    async def current(self) -> Identity: ...


def is_human_actor_id(actor_id: str) -> bool:
    """Reject reserved automation principals anywhere an API identity is used."""
    normalized = actor_id.strip().casefold()
    return bool(normalized) and not normalized.startswith(("system:", "agent:", "bridge:"))


def is_loopback_bind_host(host: str) -> bool:
    """Check literal loopback addresses without resolving names over the network."""
    normalized = host.strip().casefold()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_local_identity_bind(*, app_env: str, bind_host: str) -> None:
    """LocalIdentityProvider may serve protected environments only on loopback."""
    environment = app_env.strip().casefold()
    if environment in PROTECTED_APP_ENVIRONMENTS and not is_loopback_bind_host(bind_host):
        raise ValueError(
            "LocalIdentityProvider is unauthenticated and cannot bind outside loopback "
            "in pilot, production, or live; configure an authenticated identity provider first"
        )


class LocalIdentityProvider:
    """Unauthenticated local operator identity; use only on a loopback API bind."""

    mode = "local"
    verified = False

    def __init__(self, actor_id: str = "dev-analyst") -> None:
        self._identity = Identity(actor_id=actor_id)

    async def current(self) -> Identity:
        return self._identity


class StaticTokenIdentityProvider:
    """Minimal bearer-token operator identity for supervised pilot deployments."""

    mode = "authenticated"
    verified = True

    def __init__(self, token: str) -> None:
        if (
            len(token) < 32
            or not token.isascii()
            or any(character.isspace() for character in token)
        ):
            raise ValueError(
                "operator API token must contain at least 32 ASCII non-whitespace characters"
            )
        self._token = token.encode("ascii")
        self._identity = Identity(actor_id="operator:static-token")

    def verify_authorization(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not token
            or token.strip() != token
            or not token.isascii()
        ):
            return False
        return compare_digest(token.encode("ascii"), self._token)

    async def current(self) -> Identity:
        return self._identity
