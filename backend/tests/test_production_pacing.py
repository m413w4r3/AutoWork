from cti_app.application.production_pacing import ProductionPacingPolicy


def test_subject_delay_uses_periodic_long_cooldown() -> None:
    policy = ProductionPacingPolicy(
        subject_jitter_min_seconds=30,
        subject_jitter_max_seconds=90,
        cooldown_every_n_subjects=3,
        cooldown_min_seconds=600,
        cooldown_max_seconds=1200,
    )

    assert policy.subject_delay_ms(sequence_index=3) >= 600_000
    assert 30_000 <= policy.subject_delay_ms(sequence_index=1) <= 90_000
