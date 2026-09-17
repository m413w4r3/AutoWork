from uuid import uuid4

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.entities import Subject
from cti_app.domain.errors import TlpDowngradeError


def test_tlp_can_only_stay_equal_or_become_more_restrictive() -> None:
    subject = Subject(
        edition_id=uuid4(), title="Test subject", slug="test-subject", tlp=TLP.AMBER
    )

    subject.update_metadata(title="Test subject", tlp=TLP.RED)

    assert subject.tlp is TLP.RED
    with pytest.raises(TlpDowngradeError):
        subject.update_metadata(title="Test subject", tlp=TLP.GREEN)
    assert subject.tlp is TLP.RED
