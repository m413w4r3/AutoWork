from __future__ import annotations

import pytest

from cti_app.application.editorial_text_policy import editorial_caption_text_violation


@pytest.mark.parametrize("caption_kind", ("table", "chart", "figure"))
@pytest.mark.parametrize(
    "caption",
    (
        "ftp://example.test/a",
        "mailto:user@example.test",
        "data:text/plain,example",
        "javascript:alert(1)",
        "custom+scheme://example.test/a",
    ),
)
def test_editorial_captions_reject_non_web_url_schemes(caption_kind: str, caption: str) -> None:
    assert editorial_caption_text_violation(caption), caption_kind


@pytest.mark.parametrize("caption_kind", ("table", "chart", "figure"))
def test_editorial_captions_accept_plain_text_without_domain_false_positives(
    caption_kind: str,
) -> None:
    assert not editorial_caption_text_violation(
        f"{caption_kind.title()} comparing archived indicators at example.test/a."
    )
