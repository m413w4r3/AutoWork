from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.advisor_contracts import (
    ADVISOR_RESPONSE_SCHEMA_V3,
    ADVISOR_SOURCE_PARSER_VERSION,
    parse_typed_advisor_response,
)
from cti_app.application.advisor_dossier import (
    build_advisor_dossier,
    evaluate_advisor_evidence_preflight,
)
from cti_app.application.advisor_wire import ADVISOR_WIRE_VERSION, advisor_wire_aliases
from cti_app.application.bridge_investigator import (
    INVESTIGATION_PROMPT_VERSION,
    render_investigation_prompt,
)
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_SCHEMA_VERSION,
    InvestigationAdvisorTool,
    _safe_text,
)
from cti_app.application.investigation_contracts import (
    ArticleClaimV1,
    ArticleContextV1,
    ContextInvestigationV1,
    InvestigationContextV1,
    InvestigationSourceV1,
    parse_agent_turn_decision,
    validate_decision_hypothesis_references,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "advisor_eval"
CORPUS_PATH = FIXTURE_DIR / "anti_slop.v1.jsonl"
MANIFEST_PATH = FIXTURE_DIR / "manifest.v1.json"
CASE_SCHEMA = "advisor-anti-slop-case.v1"
FILLER_LIST_VERSION = "generic-phrases.v1"
GENERIC_PHRASES = frozenset(
    {
        "Il est recommandé d'approfondir l'analyse.",
        "Il faut rester vigilant.",
        "Further research is needed.",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_TEXT = re.compile(
    r"(?i)(?:https?://|data:[^\s,]+;base64,|\\x[0-9a-f]{2}|"
    r"(?:api[_ -]?key|private[_ -]?key|access[_ -]?token|client[_ -]?secret|"
    r"authorization|credential|password|secret)\s*[:=]|"
    r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{200,}={0,2}(?![A-Za-z0-9+/=_-]))"
)


def _walk(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")
    else:
        yield path, value


def _assert_fixture_string_safe(path: str, item: str) -> None:
    assert "real_" not in item.casefold(), path
    assert _FORBIDDEN_TEXT.search(item) is None, path
    if path.endswith(".sha256"):
        assert _SHA256.fullmatch(item), path
    else:
        assert re.search(r"(?i)(?:\b[0-9a-f]{2}\s+){8,}", item) is None, path


def _validate_contradiction_pair(case: dict[str, Any]) -> None:
    labels = case["labels"]
    source_ids = labels["contradiction_source_ids"]
    sources_by_id = {source["id"]: source for source in case["sources"]}
    assert len(source_ids) == 2
    assert set(source_ids) == set(sources_by_id)

    subject = labels["contradiction_subject"]
    property_name = labels["contradiction_property"]
    values = []
    for source_id in source_ids:
        source = sources_by_id[source_id]
        assert source["subject"] == subject
        assert source["property"] == property_name
        assert type(source["value"]) is int
        assert subject in source["text"]
        assert str(source["value"]) in source["text"]
        values.append(source["value"])
    assert len(set(values)) == 2
    assert sorted(values) == labels["contradiction_values"]

    assertions = case["context"]["assertions"]
    assert len(assertions) == 2
    for assertion in assertions:
        refs = assertion["refs"]
        assert len(refs) == 1
        source = sources_by_id[refs[0]["id"]]
        assert source["id"] in source_ids
        assert subject in assertion["text"]
        assert str(source["value"]) in assertion["text"]
        assert "size" in assertion["text"].casefold()


def test_fixture_string_guard_rejects_standalone_base64_like_text() -> None:
    with pytest.raises(AssertionError):
        _assert_fixture_string_safe("$.response_text", "A" * 200)


def _load_cases() -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    corpus_bytes = CORPUS_PATH.read_bytes()
    corpus_sha256 = hashlib.sha256(corpus_bytes).hexdigest()
    assert manifest["schema"] == "advisor-anti-slop-manifest.v1"
    assert manifest["corpus_version"] == "anti_slop.v1"
    assert manifest["corpus_sha256"] == corpus_sha256
    assert manifest["case_count"] >= 16

    lines = corpus_bytes.decode("utf-8").splitlines()
    assert lines and all(line.strip() for line in lines)
    cases = [json.loads(line) for line in lines]
    assert len(cases) == manifest["case_count"]
    assert len({case["case_id"] for case in cases}) == len(cases)
    required_classes = {
        "invalid_reference",
        "structural_anchor",
        "absence_of_evidence",
        "contradiction_unknown",
        "duplication",
        "filler",
        "synthetic_overclaim",
        "healthy_positive",
    }
    assert {case["labels"].get("class") for case in cases} == required_classes
    for class_name in required_classes:
        matching = [case for case in cases if case["labels"]["class"] == class_name]
        assert len(matching) == 2
        assert sum(case["labels"].get("negative") is True for case in matching) == 1
        assert sum(case["labels"].get("positive_control") is True for case in matching) == 1

    for case in cases:
        assert case["schema"] == CASE_SCHEMA
        assert case["synthetic_only"] is True
        assert case["consultation_type"] in {"bridge", "advisor"}
        assert isinstance(case["response_text"], str)
        assert isinstance(case["sources"], list)
        assert isinstance(case["context"], dict)
        assert isinstance(case["allowed_aliases"], list)
        assert isinstance(case["labels"], dict)
        assert isinstance(case["expected"], dict)
        assert isinstance(case["expected"].get("metrics"), dict)
        for source in case["sources"]:
            assert source["synthetic"] is True
            assert source["kind"] in {"hypothesis", "observation"}
            assert "synthetic" in source["text"].casefold()
            assert source["id"].startswith(("H-SYNTH-", "obs-synth-")) or re.fullmatch(
                r"[0-9a-f-]{36}", source["id"]
            )
        for path, item in _walk(case):
            # JSON decoding cannot produce bytes. This post-decode check only guards
            # parsed values and cannot detect raw byte payloads in the JSON source.
            assert not isinstance(item, bytes | bytearray | memoryview), path
            if isinstance(item, str):
                _assert_fixture_string_safe(path, item)
        if case["labels"].get("contradiction_targeted") is True:
            _validate_contradiction_pair(case)
    return manifest, cases, corpus_sha256


def _advisor_prompt_data(case: dict[str, Any]) -> dict[str, Any]:
    context = case["context"]
    evidence = [
        {
            "evidence_id": source["id"],
            "sha256": source["sha256"],
            "source": "synthetic.fixture",
            "object_type": "observation",
            "blob_read_verified": True,
            "content_excerpts": [
                {"kind": "text", "title": "Synthetic observation", "text": source["text"]}
            ],
        }
        for source in case["sources"]
        if source.get("kind") == "observation"
    ]
    return {
        "consultation_type": context.get("advisor_type", "draft_text"),
        "question": context.get("question", "Review synthetic evidence."),
        "evidence_refs": [item["evidence_id"] for item in evidence],
        "evidence": evidence,
        "assertions": context.get("assertions", []),
        "findings": [],
    }


def _prompt_hashes() -> dict[str, Any]:
    zero_uuid = UUID("00000000-0000-4000-8000-000000000001")
    context = InvestigationContextV1(
        view="full",
        investigation_id=zero_uuid,
        source=InvestigationSourceV1(
            subject_id=UUID("00000000-0000-4000-8000-000000000002"),
            production_run_id=UUID("00000000-0000-4000-8000-000000000003"),
            synthesis_artifact_id=UUID("00000000-0000-4000-8000-000000000004"),
            synthesis_sha256="a" * 64,
        ),
        article=ArticleContextV1(
            short_summary="Synthetic investigation prompt fixture.",
            claims=(
                ArticleClaimV1(
                    id="C-SYNTH-1",
                    statement="A synthetic claim used only to fingerprint the prompt.",
                    source_refs=("source-synth-1",),
                ),
            ),
            documented_samples=(),
            known_chain=(),
            open_questions=("Is the synthetic claim supported?",),
        ),
        investigation=ContextInvestigationV1(
            agenda=("Check synthetic claim", "Preserve uncertainty"),
            hypotheses=({"id": "H-SYNTH-1", "statement": "Synthetic hypothesis."},),
            new_evidence=(),
            findings=(),
            sample_deltas=(),
            rule_deltas=(),
            previous_actions=(),
            blocked_capabilities=(),
            budget_remaining={"bridge_turns": 0},
        ),
        available_tools=(),
    )
    investigation_prompt = render_investigation_prompt(context)

    observation_id = "obs-synth-prompt-1"
    observation_sha = "c" * 64
    advisor_data = {
        "consultation_type": "draft_text",
        "question": "Draft one synthetic fact linked to its checked source.",
        "evidence_refs": [observation_id],
        "evidence": [
            {
                "evidence_id": observation_id,
                "sha256": observation_sha,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "blob_read_verified": True,
                "content_excerpts": [
                    {
                        "kind": "text",
                        "title": "Synthetic marker",
                        "text": "A synthetic observation records one marker.",
                    }
                ],
            }
        ],
        "assertions": [
            {
                "id": "assert-synth-prompt-1",
                "text": "A synthetic observation records one marker.",
                "block_type": "key_findings",
                "form": "body",
                "anchor": None,
                "refs": [{"kind": "observation", "id": observation_id, "sha256": observation_sha}],
            }
        ],
    }
    advisor_prompt = InvestigationAdvisorTool._prompt("draft_text", advisor_data)
    return {
        "investigation": {
            "version": INVESTIGATION_PROMPT_VERSION,
            "sha256": hashlib.sha256(investigation_prompt.encode("utf-8")).hexdigest(),
        },
        "advisor": {
            "versions": {
                "tool_schema": ADVISOR_SCHEMA_VERSION,
                "wire": ADVISOR_WIRE_VERSION,
                "response_schema": ADVISOR_RESPONSE_SCHEMA_V3,
                "source_parser": ADVISOR_SOURCE_PARSER_VERSION,
            },
            "sha256": hashlib.sha256(advisor_prompt.encode("utf-8")).hexdigest(),
        },
    }


def _normalize_sentence(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.rstrip(".!?… ")


def _advisor_sentences(parsed: dict[str, Any] | None) -> list[dict[str, Any]]:
    if parsed is None:
        return []
    return [
        sentence
        for block in parsed.get("blocks", [])
        if isinstance(block, dict) and block.get("usable") is True
        for sentence in block.get("sentences", [])
        if isinstance(sentence, dict)
    ]


def _sentence_is_sha_anchored(sentence: dict[str, Any], data: dict[str, Any]) -> bool:
    assertions = {
        item.get("id"): item for item in data.get("assertions", []) if isinstance(item, dict)
    }
    sources = {
        ("observation", item.get("evidence_id")): item.get("sha256")
        for item in data.get("evidence", [])
        if isinstance(item, dict)
    }
    assertion_ids = sentence.get("assertion_ids")
    if not isinstance(assertion_ids, list) or not assertion_ids:
        return False
    for assertion_id in assertion_ids:
        assertion = assertions.get(assertion_id)
        refs = assertion.get("refs", []) if isinstance(assertion, dict) else []
        if not refs or any(
            not isinstance(ref, dict)
            or ref.get("kind") != "observation"
            or not isinstance(ref.get("id"), str)
            or not isinstance(ref.get("sha256"), str)
            or _SHA256.fullmatch(ref["sha256"]) is None
            or sources.get(("observation", ref["id"])) != ref["sha256"]
            for ref in refs
        ):
            return False
    return True


def _evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    preflight_status = "NOT_APPLICABLE"
    preflight_allows_submission = False
    if case["consultation_type"] == "bridge":
        assert case["allowed_aliases"] == sorted(
            case["context"].get("known_hypothesis_ids", [])
        )
        try:
            decision = parse_agent_turn_decision(case["response_text"])
        except ValueError:
            parser_status = "REJECTED"
            usable = False
        else:
            issue = validate_decision_hypothesis_references(
                decision,
                known_hypothesis_ids=set(case["context"].get("known_hypothesis_ids", [])),
            )
            if issue is not None:
                parser_status = "INVALID_REFERENCE"
                usable = False
            else:
                parser_status = "PARSED"
                usable = True
    else:
        data = _advisor_prompt_data(case)
        aliases = advisor_wire_aliases(data)["assertions"]
        assert case["allowed_aliases"] == list(aliases)
        dossier = build_advisor_dossier(data)
        prompt = InvestigationAdvisorTool._prompt(data["consultation_type"], data)
        preflight = evaluate_advisor_evidence_preflight(data, dossier, prompt)
        preflight_status = preflight["status"]
        preflight_allows_submission = preflight_status == "READY"
        parsed = parse_typed_advisor_response(
            case["response_text"],
            consultation_type=data["consultation_type"],
            prompt_data=data,
            safe_text=_safe_text,
        )
        if parsed is None:
            parser_status = "REJECTED"
            usable = False
        elif parsed.get("status") == "refused":
            parser_status = "REFUSED"
            usable = False
        elif parsed.get("status") == "partial":
            parser_status = "PARTIAL"
            usable = parsed.get("usable") is True
        else:
            parser_status = "PARSED"
            usable = parsed.get("usable") is True

    advisor_sentences = _advisor_sentences(parsed)
    labels = case["labels"]
    factual_markers = Counter(
        _normalize_sentence(item) for item in labels.get("fact_sentences", [])
    )
    remaining_fact_markers = factual_markers.copy()
    factual_count = 0
    anchored_count = 0
    for sentence in advisor_sentences:
        sentence_text = sentence.get("text")
        if not isinstance(sentence_text, str):
            continue
        sentence_key = _normalize_sentence(sentence_text)
        if remaining_fact_markers[sentence_key] <= 0:
            continue
        remaining_fact_markers[sentence_key] -= 1
        factual_count += 1
        anchored_count += int(_sentence_is_sha_anchored(sentence, data or {}))
    factual_matches = Counter(
        _normalize_sentence(sentence["text"])
        for sentence in advisor_sentences
        if isinstance(sentence.get("text"), str)
        and factual_markers[_normalize_sentence(sentence["text"])] > 0
    )
    duplicate_count = sum(max(0, count - 1) for count in factual_matches.values())
    filler_count = sum(
        1
        for sentence in advisor_sentences
        if _normalize_sentence(str(sentence.get("text", "")))
        in {_normalize_sentence(item) for item in GENERIC_PHRASES}
    )

    no_proof_unsafe = (
        bool(usable or preflight_allows_submission) if labels.get("no_proof") is True else None
    )
    contradiction_unknown: bool | None = None
    if labels.get("contradiction_targeted") is True:
        assertion_by_id = {
            item.get("id"): item
            for item in (data or {}).get("assertions", [])
            if isinstance(item, dict)
        }
        cited_sources = {
            ref.get("id")
            for sentence in advisor_sentences
            for assertion_id in sentence.get("assertion_ids", [])
            for ref in assertion_by_id.get(assertion_id, {}).get("refs", [])
            if isinstance(ref, dict)
        }
        visible_text = " ".join(
            [str(item.get("text", "")) for item in advisor_sentences]
            + [str(item) for item in (parsed or {}).get("limitations", [])]
        )
        required_sources = set(labels.get("contradiction_source_ids", []))
        explicit_unknown = re.search(r"(?i)\bunknown\b|\binconnu(?:e)?\b", visible_text)
        explicit_limit = re.search(
            r"(?i)\blimit(?:e|ation|ations|ed)\w*\b|\bportée\b", visible_text
        )
        contradiction_unknown = bool(
            usable
            and len(required_sources) >= 2
            and required_sources.issubset(cited_sources)
            and all(
                str(value) in visible_text
                for value in labels.get("contradiction_values", [])
            )
            and explicit_unknown
            and explicit_limit
        )

    observed_metrics = {
        "invalid_reference_usable": (
            bool(usable) if labels.get("invalid_reference") is True else None
        ),
        "factual_sentence_count": factual_count,
        "anchored_factual_sentence_count": anchored_count,
        "no_proof_unsafe": no_proof_unsafe,
        "contradiction_unknown": contradiction_unknown,
        "duplicate_factual_sentence_count": duplicate_count,
        "generic_clause_count": filler_count,
        "overclaim_usable": (
            bool(usable) if labels.get("synthetic_overclaim") is True else None
        ),
        "healthy_positive": (
            bool(
                usable
                and preflight_status == "READY"
                and factual_count > 0
                and anchored_count == factual_count
                and duplicate_count == 0
                and filler_count == 0
            )
            if labels.get("healthy_positive") is True
            else None
        ),
    }
    return {
        "case_id": case["case_id"],
        "class": labels["class"],
        "parser_status": parser_status,
        "preflight_status": preflight_status,
        "usable": bool(usable),
        "metrics": observed_metrics,
        "factual_sentence_count": factual_count,
        "anchored_factual_sentence_count": anchored_count,
        "duplicate_factual_sentence_count": duplicate_count,
        "generic_clause_count": filler_count,
        "advisor_clause_count": len(advisor_sentences),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def test_versioned_anti_slop_corpus_has_deterministic_offline_oracle(capsys) -> None:
    variant = os.environ.get("ANTI_SLOP_EVAL_VARIANT", "baseline")
    assert variant in {"baseline", "patch"}
    manifest, cases, corpus_sha256 = _load_cases()
    results = [_evaluate_case(case) for case in cases]
    result_by_id = {item["case_id"]: item for item in results}

    for case in cases:
        actual = result_by_id[case["case_id"]]
        expected = case["expected"]
        assert actual["parser_status"] == expected["parser_status"], case["case_id"]
        assert actual["preflight_status"] == expected["preflight_status"], case["case_id"]
        assert actual["usable"] is expected["usable"], case["case_id"]
        assert actual["metrics"] == expected["metrics"], case["case_id"]

    classes: dict[str, dict[str, Any]] = {}
    for result in results:
        group = classes.setdefault(
            result["class"], {"cases": 0, "usable": 0, "preflight": Counter()}
        )
        group["cases"] += 1
        group["usable"] += int(result["usable"])
        group["preflight"][result["preflight_status"]] += 1

    invalid_cases = [case for case in cases if case["labels"].get("invalid_reference") is True]
    invalid_usable = sum(result_by_id[case["case_id"]]["usable"] for case in invalid_cases)
    factual_total = sum(item["factual_sentence_count"] for item in results)
    anchored_total = sum(item["anchored_factual_sentence_count"] for item in results)
    no_proof_cases = [case for case in cases if case["labels"].get("no_proof") is True]
    no_proof_unsafe = sum(
        bool(result_by_id[case["case_id"]]["metrics"]["no_proof_unsafe"])
        for case in no_proof_cases
    )
    contradiction_cases = [
        case for case in cases if case["labels"].get("contradiction_targeted") is True
    ]
    contradiction_ok = sum(
        bool(result_by_id[case["case_id"]]["metrics"]["contradiction_unknown"])
        for case in contradiction_cases
    )
    duplicate_controls = [
        case
        for case in cases
        if case["labels"].get("class") == "duplication"
        and case["labels"].get("positive_control") is True
    ]
    duplicate_count = sum(
        result_by_id[case["case_id"]]["duplicate_factual_sentence_count"]
        for case in duplicate_controls
    )
    duplicate_facts = sum(
        result_by_id[case["case_id"]]["factual_sentence_count"] for case in duplicate_controls
    )
    filler_controls = [
        case
        for case in cases
        if case["labels"].get("class") == "filler"
        and case["labels"].get("positive_control") is True
    ]
    filler_count = sum(
        result_by_id[case["case_id"]]["generic_clause_count"] for case in filler_controls
    )
    filler_clauses = sum(
        result_by_id[case["case_id"]]["advisor_clause_count"] for case in filler_controls
    )
    overclaim_cases = [
        case for case in cases if case["labels"].get("synthetic_overclaim") is True
    ]
    overclaim_usable = sum(
        result_by_id[case["case_id"]]["usable"] for case in overclaim_cases
    )
    healthy_cases = [case for case in cases if case["labels"].get("healthy_positive") is True]
    healthy_ok = sum(
        bool(result_by_id[case["case_id"]]["metrics"]["healthy_positive"])
        for case in healthy_cases
    )

    metrics = {
        "invalid_references": {
            "numerator": invalid_usable,
            "denominator": len(invalid_cases),
            "rate": _rate(invalid_usable, len(invalid_cases)),
            "threshold": 0.0,
        },
        "structural_anchoring": {
            "numerator": anchored_total,
            "denominator": factual_total,
            "rate": _rate(anchored_total, factual_total),
            "threshold": 1.0,
        },
        "absence_of_proof": {
            "numerator": no_proof_unsafe,
            "denominator": len(no_proof_cases),
            "rate": _rate(no_proof_unsafe, len(no_proof_cases)),
            "threshold": 0.0,
        },
        "contradiction_unknown": {
            "numerator": contradiction_ok,
            "denominator": len(contradiction_cases),
            "rate": _rate(contradiction_ok, len(contradiction_cases)),
            "threshold": 1.0,
        },
        "duplication_positive_controls": {
            "numerator": duplicate_count,
            "denominator": duplicate_facts,
            "rate": _rate(duplicate_count, duplicate_facts),
            "threshold": 0.0,
        },
        "generic_filler_positive_controls": {
            "numerator": filler_count,
            "denominator": filler_clauses,
            "rate": _rate(filler_count, filler_clauses),
            "threshold": 0.0,
        },
        "synthetic_overclaim": {
            "numerator": overclaim_usable,
            "denominator": len(overclaim_cases),
            "rate": _rate(overclaim_usable, len(overclaim_cases)),
            "threshold": 0.0,
        },
        "healthy_positive_controls": {
            "numerator": healthy_ok,
            "denominator": len(healthy_cases),
            "rate": _rate(healthy_ok, len(healthy_cases)),
            "threshold": 1.0,
        },
    }
    class_metric_names = {
        "absence_of_evidence": ("absence_of_proof",),
        "contradiction_unknown": ("contradiction_unknown",),
        "duplication": ("duplication_positive_controls",),
        "filler": ("generic_filler_positive_controls",),
        "healthy_positive": ("healthy_positive_controls",),
        "invalid_reference": ("invalid_references",),
        "structural_anchor": ("structural_anchoring",),
        "synthetic_overclaim": ("synthetic_overclaim",),
    }
    per_class_metrics = {}
    for class_name, metric_names in class_metric_names.items():
        if class_name == "structural_anchor":
            class_case_ids = {
                case["case_id"] for case in cases if case["labels"]["class"] == class_name
            }
            class_facts = sum(
                result_by_id[case_id]["factual_sentence_count"] for case_id in class_case_ids
            )
            class_anchors = sum(
                result_by_id[case_id]["anchored_factual_sentence_count"]
                for case_id in class_case_ids
            )
            per_class_metrics[class_name] = {
                "structural_anchoring": {
                    "numerator": class_anchors,
                    "denominator": class_facts,
                    "rate": _rate(class_anchors, class_facts),
                    "threshold": 1.0,
                }
            }
        else:
            per_class_metrics[class_name] = {
                metric_name: metrics[metric_name] for metric_name in metric_names
            }
    assert invalid_usable == 0
    assert factual_total > 0 and anchored_total == factual_total
    assert no_proof_unsafe == 0
    assert contradiction_ok == len(contradiction_cases) > 0
    assert duplicate_count == 0
    assert filler_count == 0
    assert overclaim_usable == 0
    assert healthy_ok == len(healthy_cases) > 0

    report = {
        "schema": "advisor-anti-slop-report.v1",
        "variant": variant,
        "corpus_version": manifest["corpus_version"],
        "case_count": len(cases),
        "corpus_sha256": corpus_sha256,
        "prompt_fingerprints": _prompt_hashes(),
        "filler_list_version": FILLER_LIST_VERSION,
        "cases": [
            {
                "case_id": case["case_id"],
                "class": case["labels"]["class"],
                "expected": {
                    "parser_status": case["expected"]["parser_status"],
                    "preflight_status": case["expected"]["preflight_status"],
                    "usable": case["expected"]["usable"],
                    "metrics": case["expected"]["metrics"],
                },
                "observed": {
                    "parser_status": result_by_id[case["case_id"]]["parser_status"],
                    "preflight_status": result_by_id[case["case_id"]]["preflight_status"],
                    "usable": result_by_id[case["case_id"]]["usable"],
                    "metrics": result_by_id[case["case_id"]]["metrics"],
                },
            }
            for case in cases
        ],
        "classes": {
            name: {
                "cases": group["cases"],
                "usable": group["usable"],
                "preflight": dict(sorted(group["preflight"].items())),
                "metrics": per_class_metrics[name],
            }
            for name, group in sorted(classes.items())
        },
        "metrics": metrics,
    }
    assert report["variant"] == variant
    assert len(report["cases"]) == len(cases)
    assert [item["case_id"] for item in report["cases"]] == [
        case["case_id"] for case in cases
    ]
    assert all(item["expected"] == item["observed"] for item in report["cases"])
    with capsys.disabled():
        print("ANTI_SLOP_EVAL " + json.dumps(report, ensure_ascii=False, sort_keys=True))
