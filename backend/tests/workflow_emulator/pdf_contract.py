"""Reader-facing PDF labels shared by the workflow emulator checks.

The Typst reader templates print the analyst-opinion panel through the CHP
``noteAnalyste`` component, whose reader-facing title is "Note de l'analyste"
(``chpTypst/UTILS/helpers.typ``); the render-data projection uses the same
label. Keep every PDF assertion on this one constant so a label change is a
single edit instead of a search across scenarios.
"""

from __future__ import annotations

from cti_app.application.typst_rendering import _protocol_figure_brief
from cti_app.domain.analyst_opinion import OpinionProtocolFigureV1

# Case-folded form compared against extracted, case-folded PDF text.
ANALYST_OPINION_HEADING = "note de l'analyste"


def fold_pdf_text(text: str) -> str:
    """Case-fold extracted PDF text and unify the Typst typographic apostrophe."""
    return text.casefold().replace(chr(0x2019), "'")


def rendered_protocol_caption(figure: OpinionProtocolFigureV1) -> str:
    """Return the reader-facing caption the Typst template prints for a figure.

    ``_protocol_figure_brief`` is the single projection that turns the stored
    protocol caption into reader labels; the template prints its caption (or
    its title when the caption is empty). Deriving the expectation from that
    projection keeps the PDF assertion exact without duplicating the mapping.
    """
    title, caption, _status_label = _protocol_figure_brief(figure, is_draft_preview=False)
    return caption or title
