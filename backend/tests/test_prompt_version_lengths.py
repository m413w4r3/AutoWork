import cti_app.application.production_editorial_enrichment as editorial_enrichment
import cti_app.application.production_prompts as prompts
import cti_app.application.production_synthesis as synthesis
import cti_app.application.production_synthesis_revision as synthesis_revision
import cti_app.domain.production_editorial_enrichment as enrichment_domain
import cti_app.domain.semantic_annotation as semantic_annotation_domain

# model_runs.prompt_template_version (and similar policy columns) are varchar(64).
_MAX_VERSION_LENGTH = 64


def test_prompt_and_contract_version_constants_fit_database_columns() -> None:
    all_prompt_and_domain_modules = (prompts, enrichment_domain, editorial_enrichment)
    targeted_version_constants = {
        synthesis: {
            "SYNTHESIS_VALIDATOR_VERSION",
            "ARTICLE_SYNTHESIS_CORRECTIVE_PROMPT_VERSION",
        },
        synthesis_revision: {"ARTICLE_TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION"},
        editorial_enrichment: {
            "EDITORIAL_ENRICHMENT_GENERATOR_VERSION",
            "EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION",
            "SEMANTIC_ANNOTATION_PROMPT_VERSION",
            "SEMANTIC_ANNOTATION_CONTRACT_VERSION",
            "SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION",
        },
        semantic_annotation_domain: {
            "SEMANTIC_ANNOTATION_SCHEMA_VERSION",
            "SEMANTIC_ANNOTATION_POLICY_VERSION",
        },
    }
    too_long = {
        name: value
        for module in all_prompt_and_domain_modules
        for name, value in vars(module).items()
        if isinstance(value, str)
        and name.isupper()
        and name.endswith(("_VERSION", "_CONTRACT_VERSION", "_PARSER_VERSION"))
        and len(value) > _MAX_VERSION_LENGTH
    }
    too_long.update(
        {
            f"{module.__name__}.{name}": value
            for module, names in targeted_version_constants.items()
            for name in names
            if isinstance(value := getattr(module, name), str) and len(value) > _MAX_VERSION_LENGTH
        }
    )

    assert too_long == {}
