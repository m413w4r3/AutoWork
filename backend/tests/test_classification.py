import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.entities import Subject
from cti_app.domain.errors import TlpDowngradeError


def test_tlp_can_only_stay_equal_or_become_more_restrictive() -> None:
    subject = Subject(external_id="SUBJ-TEST-1", slug="test-subject", tlp=TLP.AMBER)

    subject.restrict_tlp(TLP.RED)

    assert subject.tlp is TLP.RED
    with pytest.raises(TlpDowngradeError):
        subject.restrict_tlp(TLP.GREEN)
    assert subject.tlp is TLP.RED
