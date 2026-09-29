"""Renderer boundary for a canonical publication document."""

from __future__ import annotations

from typing import Protocol

from cti_app.domain.publication import PublicationDocumentV3


class PublicationRenderer(Protocol):
    def render(self, document: PublicationDocumentV3) -> str: ...
