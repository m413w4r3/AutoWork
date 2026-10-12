"""Keep the inline CI renderer smoke payload aligned with the renderer contract."""

import json
import re
from pathlib import Path

from cti_app.application.typst_rendering import _RENDER_DATA_SCHEMA_VERSION

_CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_HEREDOC = re.compile(r"RENDERER/render-data\.json\" <<EOF\n(?P<payload>.*?)\n\s*EOF", re.DOTALL)


def test_ci_smoke_render_data_matches_the_renderer_contract() -> None:
    match = _HEREDOC.search(_CI_WORKFLOW.read_text(encoding="utf-8"))
    assert match is not None, "render-data.json heredoc not found in ci.yml"

    payload = json.loads(match.group("payload"))

    assert payload["schema_version"] == _RENDER_DATA_SCHEMA_VERSION
    assert isinstance(payload["content_sections"], list)
    assert payload["content_sections"], "the smoke payload must render at least one section"
