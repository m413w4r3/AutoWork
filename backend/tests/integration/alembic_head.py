"""The Alembic head read from the migration scripts (never a hard-coded revision).

Lot-level migration tests only assert "the database is at the head after upgrade", so
they must follow the chain instead of pinning the revision their own lot created.
``tests/test_alembic_chain.py`` remains the single place that pins the exact chain.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def current_head() -> str:
    scripts = ScriptDirectory.from_config(Config(_BACKEND_ROOT / "alembic.ini"))
    head = scripts.get_current_head()
    if head is None:  # pragma: no cover - a repository without migrations is not supported
        raise RuntimeError("no Alembic head found")
    return head
