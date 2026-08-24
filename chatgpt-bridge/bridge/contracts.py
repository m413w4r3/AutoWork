"""Modèles/types de contrat du bridge (MOVE-ONLY depuis server.py)."""

import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------- #
# Modèles de requête (sous-ensemble utile de l'API OpenAI)
# --------------------------------------------------------------------------- #
class ChatMessage(BaseModel):
    role: str = "user"
    # str, ou liste de blocs multimodaux [{"type": "text", "text": "..."}]
    content: Any = ""
    name: Optional[str] = None


class FileAttachment(BaseModel):
    """Pièce jointe déposée dans le composer avant l'envoi du prompt."""

    name: str
    mime: str = "application/octet-stream"
    data: str = Field(description="Contenu du fichier encodé en base64")


class BridgeConversationTarget(BaseModel):
    """Cible applicative explicite ; le locator reste opaque après validation d'origine."""

    mode: str
    id: uuid.UUID
    external_locator: Optional[str] = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.mode not in {"fresh", "continue"}:
            raise ValueError("conversation.mode doit valoir fresh ou continue")
        if self.mode == "fresh" and self.external_locator is not None:
            raise ValueError("fresh interdit un locator préexistant")
        if self.mode == "continue" and not self.external_locator:
            raise ValueError("continue exige external_locator")
        if self.external_locator:
            parsed = urlsplit(self.external_locator)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"chatgpt.com", "chat.openai.com"}
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
                or parsed.fragment
                or parsed.path in {"", "/"}
            ):
                raise ValueError("locator hors des origines ChatGPT autorisées")
        return self


class ChatRequest(BaseModel):
    model: str = "chatgpt-web"
    messages: List[ChatMessage]
    stream: bool = False
    # Extension maison : ouvre un nouveau chat avant d'envoyer le prompt.
    new_chat: bool = Field(default=False, description="Repart d'une conversation vierge")
    files: List[FileAttachment] = Field(default_factory=list, description="Pièces jointes")

    # Les paramètres OpenAI sans équivalent dans l'UI web sont acceptés puis ignorés.
    model_config = {"extra": "allow"}


class ResponseRequest(BaseModel):
    """Sous-ensemble Responses API supporté par le bridge local.

    Le bridge traduit ces champs vers l'UI ChatGPT. Il ne prétend pas fournir
    les garanties natives du service OpenAI : le client doit revalider les
    sorties structurées et conserver ses propres ModelRun.
    """

    model: str = "chatgpt-web"
    input: Any
    tools: List[dict] = Field(default_factory=list)
    include: List[str] = Field(default_factory=list)
    text: Optional[dict] = None
    background: bool = False
    stream: bool = False
    conversation: Optional[BridgeConversationTarget] = None
    bridge_recovery: bool = False

    model_config = {"extra": "allow"}


# Modèle « demandé » qui signifie en réalité « ne touche pas au sélecteur de
# l'UI » : ces identifiants ne désignent aucun modèle réel de l'interface.
MODELES_NEUTRES = {"", "chatgpt-web", "auto", "default"}


class BridgeRunRequest(BaseModel):
    """Contrat natif du bridge, distinct des garanties de Responses API."""

    # Étiquette de traçabilité de l'appelant (nom de profil applicatif, modèle
    # d'API…). Elle ne pilote rien : l'UI ChatGPT ne connaît pas ces noms.
    requested_model: str = "chatgpt-web"
    input: Any
    web_search: bool = False
    response_format: Optional[dict] = None
    reasoning_effort: Optional[str] = None
    background: bool = False
    # Entrée du sélecteur de modèle de l'UI à appliquer et vérifier, elle.
    ui_model: Optional[str] = None
    # Profil / espace de travail ChatGPT à sélectionner avant la génération.
    profile: Optional[str] = None
    # Par défaut, un modèle demandé mais non vérifié fait échouer le run : mieux
    # vaut une erreur qu'un run attribué au mauvais modèle dans la traçabilité CTI.
    allow_unverified_model: bool = False
    # Identité stable fournie par l'application. L'en-tête HTTP équivalent est
    # prioritaire, mais les deux doivent concorder lorsqu'ils sont présents.
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=255)
    conversation: Optional[BridgeConversationTarget] = None
    recovery: bool = False


class RunControls(BaseModel):
    """Réglages d'interface à appliquer avant une génération.

    `None` signifie « laisse tel quel » ; c'est distinct de `False`, qui exige
    au contraire de désactiver le réglage.
    """

    model: Optional[str] = None
    profile: Optional[str] = None
    web_search: Optional[bool] = None

    def wanted(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class ConversationReleaseRequest(BaseModel):
    """Explicit release of a conversation with an outcome.

    Only the client decides when a conversation is no longer needed,
    and the outcome of that release.
    """

    outcome: str = Field(..., pattern="^(success|failure|needs_review|cancelled)$")


class ConversationLifecycleResponse(BaseModel):
    """Current lifecycle status of a conversation."""

    conversation_id: str
    policy: str
    status: str
    release_outcome: Optional[str] = None
    created_at: float
    updated_at: float
    released_at: Optional[float] = None
    deleted_at: Optional[float] = None
    cleanup_attempt_count: int
    last_cleanup_attempt_at: Optional[float] = None
    last_cleanup_error_code: Optional[str] = None
    version: int


class ControlOutcome(BaseModel):
    """Résultat d'un contrôle, tel que le content script l'a *relu* dans le DOM."""

    requested: Any = None
    applied: Any = None
    verified: bool = False
    ok: bool = False
    changed: bool = False
    reason: Optional[str] = None

    model_config = {"extra": "allow"}


# Résultats de contrôles, indexés par nom de réglage (« model », « web_search »…).
Outcomes = Dict[str, ControlOutcome]


class UiPickerState(BaseModel):
    """Sélecteur à déclencheur (modèle, profil)."""

    supported: bool = False
    selected: Optional[str] = None
    selected_id: Optional[str] = None
    verified: bool = False
    available: Optional[List[dict]] = None
    reason: Optional[str] = None


class UiWebSearchState(BaseModel):
    # `supported: None` = indéterminé sans ouvrir le menu d'outils (sonde).
    supported: Optional[bool] = None
    enabled: Optional[bool] = None
    verified: bool = False
    via: Optional[str] = None
    reason: Optional[str] = None


class UiState(BaseModel):
    """État pilotable de l'onglet ChatGPT, observé par le content script."""

    observed_at: Optional[float] = None
    url: Optional[str] = None
    content_script_version: Optional[str] = None
    probed: bool = False
    model: UiPickerState = Field(default_factory=UiPickerState)
    profile: UiPickerState = Field(default_factory=UiPickerState)
    web_search: UiWebSearchState = Field(default_factory=UiWebSearchState)

    model_config = {"extra": "allow"}


class RunReport(BaseModel):
    """Ce que le bridge a réellement obtenu de l'UI pour un run donné."""

    model_observed: Optional[str] = None
    model_source: str = "unknown"
    web_search_mode: str = "untouched"
    controls: Outcomes = Field(default_factory=dict)

    # `model_*` est un espace de noms réservé par pydantic ; ici ce sont bien
    # des champs de données, pas de la configuration.
    model_config = {"protected_namespaces": ()}


class CleanupStartRequest(BaseModel):
    """Request to start cleanup of a DELETE_PENDING conversation."""
    pass


class CleanupStartResponse(BaseModel):
    """Response after initiating cleanup."""
    conversation_id: str
    status: str  # Should be DELETING if cleanup started
    cleanup_attempt_count: int


class CleanupFailureRequest(BaseModel):
    """Report cleanup failure for retry handling."""
    error_code: str = Field(..., pattern="^[a-z_]+$", max_length=64)
    error_message: Optional[str] = None
