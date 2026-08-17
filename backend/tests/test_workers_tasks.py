from cti_app.application.discovery import DISCOVERY_JOB_KIND
from cti_app.config import get_settings
from cti_app.workers.tasks import DURABLE_RESUME_JOB_KINDS, execute_job


def test_execute_job_outlives_the_dramatiq_default_time_limit() -> None:
    # Sans time_limit explicite, Dramatiq tue l'actor après 600 000 ms, en
    # plein milieu d'une recherche ChatGPT durable.
    assert execute_job.options["time_limit"] > 900_000
    assert execute_job.options["time_limit"] == int(
        get_settings().job_actor_time_limit_seconds * 1000
    )


def test_discovery_is_declared_durable_for_the_recovery_process() -> None:
    assert DISCOVERY_JOB_KIND in DURABLE_RESUME_JOB_KINDS
