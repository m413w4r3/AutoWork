from dataclasses import dataclass
from enum import StrEnum
from hmac import compare_digest
from ipaddress import ip_address
from typing import Protocol

PROTECTED_APP_ENVIRONMENTS = frozenset({"pilot", "prod", "production", "live"})


class IdentityScope(StrEnum):
    HUMAN_OPERATOR = "human_operator"
    OPERATOR_CLIENT = "operator_client"


@dataclass(frozen=True, slots=True)
class Identity:
    actor_id: str
    scopes: frozenset[IdentityScope] = frozenset({IdentityScope.HUMAN_OPERATOR})

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")
        if any(not isinstance(scope, IdentityScope) for scope in self.scopes):
            raise TypeError("Identity scopes must use IdentityScope")
        object.__setattr__(self, "scopes", frozenset(self.scopes))

    def has_scope(self, scope: IdentityScope) -> bool:
        return scope in self.scopes


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

    def __init__(self, token: str | None, *, operator_client_token: str | None = None) -> None:
        for value, label in (
            (token, "operator API token"),
            (operator_client_token, "operator client API token"),
        ):
            if value is not None and (
                len(value) < 32
                or not value.isascii()
                or any(character.isspace() for character in value)
            ):
                raise ValueError(
                    f"{label} must contain at least 32 ASCII non-whitespace characters"
                )
        if token is not None and operator_client_token is not None:
            if compare_digest(token.encode("ascii"), operator_client_token.encode("ascii")):
                raise ValueError("Human and operator client API tokens must be distinct")
        self._token = token.encode("ascii") if token is not None else None
        self._operator_client_token = (
            operator_client_token.encode("ascii") if operator_client_token is not None else None
        )
        self._identity = Identity(actor_id="operator:static-token")
        self._operator_client_identity = Identity(
            actor_id="operator:operator-client",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )

    def verify_authorization(self, authorization: str | None) -> bool:
        return self.identity_for_authorization(authorization) is not None

    def identity_for_authorization(self, authorization: str | None) -> Identity | None:
        if not authorization:
            return None
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not token
            or token.strip() != token
            or not token.isascii()
        ):
            return None
        supplied = token.encode("ascii")
        human_match = self._token is not None and compare_digest(supplied, self._token)
        client_match = self._operator_client_token is not None and compare_digest(
            supplied, self._operator_client_token
        )
        if client_match:
            return self._operator_client_identity
        return self._identity if human_match else None

    async def current(self) -> Identity:
        return self._identity
