"""The state-machine legend names documented states that have no specified transition."""

from __future__ import annotations

from dataclasses import replace

from cti_app.application.protocol_diagram_projection import protocol_diagram_caption
from cti_app.domain.protocol_model import ProtocolDiagramKind
from tests.test_protocol_diagrams import _three_views


def test_documented_state_legend_includes_isolated_state_sentence() -> None:
    state_machine = next(
        item for item in _three_views() if item.kind is ProtocolDiagramKind.STATE_MACHINE
    )
    spec = replace(
        state_machine,
        title="États du protocole",
        limitations=(
            "ARTICLE_DOCUMENTED : états et transitions tirés des sources.",
            "L'état Fermée est documenté sans transition spécifiée et n'est relié par aucune "
            "flèche.",
        ),
    )

    caption = protocol_diagram_caption(spec)

    assert "sans transition spécifiée" in caption
    assert "Fermée" in caption
