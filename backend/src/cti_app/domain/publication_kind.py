"""Canonical publication format shared by routing, publication and edition."""

from __future__ import annotations

from enum import StrEnum


class PublicationKind(StrEnum):
    ARTICLE = "article"
    BRIEF = "brief"


__all__ = ["PublicationKind"]
