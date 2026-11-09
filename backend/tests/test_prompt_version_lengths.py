import cti_app.application.production_prompts as prompts
import cti_app.domain.production_editorial_enrichment as enrichment_domain

# model_runs.prompt_template_version (and similar policy columns) are varchar(64).
_MAX_VERSION_LENGTH = 64


def test_prompt_and_contract_version_constants_fit_database_columns() -> None:
    too_long = {
        name: value
        for module in (prompts, enrichment_domain)
        for name, value in vars(module).items()
        if isinstance(value, str)
        and name.isupper()
        and name.endswith(("_VERSION", "_CONTRACT_VERSION", "_PARSER_VERSION"))
        and len(value) > _MAX_VERSION_LENGTH
    }

    assert too_long == {}
