from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application import advisor_wire as advisor_wire_module
from cti_app.application.advisor_contracts import (
    extract_json_object,
    parse_typed_advisor_response,
)
from cti_app.application.advisor_dossier import build_advisor_dossier, extract_content_excerpts
from cti_app.application.advisor_prompt import render_advisor_prompt
from cti_app.application.advisor_wire import (
    advisor_wire_aliases,
    parse_advisor_wire,
    render_advisor_wire_input,
)
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_RESPONSE_SCHEMA,
    _consultation_article_passages,
    _parse_advisor_output,
    _safe_text,
)
from cti_app.domain.advisor_figure_plan import AdvisorFigurePlanV1

_ARCHIVED_RUN12_CORPUS = (
    Path(__file__).parents[2]
    / ".autowork-runs"
    / "run12-editorial-2026-10-20"
    / "bridge-comms"
    / "corpus"
)


def test_wire_dossier_renders_relevant_measurement_with_byte_provenance() -> None:
    prompt_data = {
        "question": "Vérifier les octets observés autour de l'offset 15232.",
        "consultation_type": "yara_draft",
        "measurements": [
            {
                "measurement_id": "m001",
                "evidence_sha256": "a" * 64,
                "sample_sha256": "1" * 64,
                "offset": 0,
                "length": 2,
                "kind": "hex",
                "encoding": "raw",
                "value": "e584",
            },
            {
                "measurement_id": "m002",
                "evidence_sha256": "a" * 64,
                "sample_sha256": "1" * 64,
                "offset": 15232,
                "length": 2,
                "kind": "hex",
                "encoding": "raw",
                "value": "1944",
            },
        ],
    }

    rendered = render_advisor_wire_input(prompt_data)

    assert "[E1]" in rendered
    fragment_sha256 = hashlib.sha256(bytes.fromhex("1944")).hexdigest()
    assert (
        "hex, offset 15232; encoding=hex; byte_length=2; length=2; sha256="
        + fragment_sha256
        + "; fragment_sha256="
        + fragment_sha256
        + ": 1944"
    ) in rendered
    assert "offset 0" not in rendered
    assert "measurement_id" not in rendered
    assert "a" * 64 not in rendered
    assert "1" * 64 not in rendered


def test_advisor_dossier_preserves_large_authorized_evidence_without_a_size_cap() -> None:
    excerpt = ("Canonical static analysis marker with complete source text.\n" * 40_000) + (
        "FINAL-SOURCE-MARKER"
    )
    prompt_data = {
        "consultation_type": "plan_investigation",
        "question": "Review the canonical static analysis marker.",
        "transmitted_refs": [
            {"kind": "observation", "id": "observation-large", "sha256": "a" * 64}
        ],
        "evidence": [
            {
                "evidence_id": "observation-large",
                "sha256": "a" * 64,
                "source": "static-analysis",
                "content_excerpts": [
                    {"kind": "text", "title": "Complete tool output", "text": excerpt}
                ],
            }
        ],
    }

    dossier = build_advisor_dossier(prompt_data)

    assert len(excerpt.encode("utf-8")) > 1_000_000
    assert dossier.byte_size > 1_000_000
    assert dossier.text.count("Canonical static analysis marker") == 40_000
    assert dossier.text.rsplit("\n", 1)[-1].endswith("FINAL-SOURCE-MARKER")


def test_content_excerpt_extraction_keeps_whole_methods_decoded_values_and_offset_hex() -> None:
    method = "private void MapImage(byte[] image) {\n    MapSections(image);\n}"
    excerpts = extract_content_excerpts(
        {
            "schema": "dotnet-analysis-observation.v1",
            "assembly": "Blackwood.dll",
            "sha256": "a" * 64,
            "methods": [{"name": "MapImage", "body": method}],
            "decoded_strings": [{"name": "ReflectiveLoader", "decoded_value": "VirtualAlloc"}],
            "hex": [{"offset": 128, "value": "4D 5A 90 00"}],
        }
    )

    assert {item["kind"] for item in excerpts} == {"method", "decoded", "hex"}
    assert next(item for item in excerpts if item["kind"] == "method")["text"] == method
    assert next(item for item in excerpts if item["kind"] == "decoded")["text"] == "VirtualAlloc"
    hex_excerpt = next(item for item in excerpts if item["kind"] == "hex")
    assert hex_excerpt["title"].endswith("offset 128")
    assert hex_excerpt["text"] == "4D 5A 90 00"
    assert all("a" * 64 not in item["text"] for item in excerpts)


def test_hex_excerpt_preserves_full_and_malformed_values_with_offsets() -> None:
    excerpts = extract_content_excerpts(
        {
            "hex": [
                {"offset": 64, "value": "AA BB"},
                {"offset": 128, "value": "AA BB"},
                {"offset": 128, "value": "AA" * 513},
                {"offset": 192, "value": "zz00"},
                {"offset": 256, "value": "A0xA"},
            ]
        }
    )

    assert len(excerpts) == 5
    assert [excerpt["title"].rsplit("offset ", 1)[-1] for excerpt in excerpts] == [
        "64",
        "128",
        "128",
        "192",
        "256",
    ]
    assert excerpts[0]["text"] == "AA BB"
    assert excerpts[1]["text"] == "AA BB"
    assert excerpts[2]["text"] == "AA" * 513
    assert excerpts[3]["text"] == "zz00"
    assert excerpts[4]["text"] == "A0xA"


def test_split_hex_dump_over_aggregate_binary_budget_is_preserved() -> None:
    excerpts = extract_content_excerpts(
        {
            "hex": [
                {"offset": 64, "value": "AA" * 257},
                {"offset": 321, "value": "BB" * 256},
            ]
        }
    )

    assert [excerpt["text"] for excerpt in excerpts] == ["AA" * 257, "BB" * 256]
    assert [excerpt["title"].rsplit("offset ", 1)[-1] for excerpt in excerpts] == ["64", "321"]


def test_advisor_bridge_provenance_keeps_internal_measurement_ids_and_exact_bytes() -> None:
    from cti_app.application.investigation_advisor_tools import _advisor_bridge_measurement_catalog

    prompt_data = {
        "measurements": [
            {
                "measurement_id": "m001",
                "evidence_sha256": "a" * 64,
                "sample_sha256": "1" * 64,
                "offset": 15232,
                "length": 2,
                "kind": "hex",
                "encoding": "raw",
                "value": "1944",
            }
        ]
    }

    catalog = _advisor_bridge_measurement_catalog(prompt_data)

    assert catalog == [
        {
            "alias": "M1",
            "measurement_id": "m001",
            "sha256": hashlib.sha256(
                json.dumps(
                    prompt_data["measurements"][0],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "evidence_sha256": "a" * 64,
            "sample_sha256": "1" * 64,
            "offset": 15232,
            "length": 2,
            "kind": "hex",
            "encoding": "raw",
            "value": "1944",
        }
    ]


def test_dossier_manifest_resolves_human_evidence_labels_and_historical_refs() -> None:
    observation_id = str(uuid4())
    prompt_data = {
        "question": "Comparer la méthode de mappage PE.",
        "consultation_type": "review_text",
        "transmitted_refs": [{"kind": "observation", "id": observation_id, "sha256": "a" * 64}],
        "evidence": [
            {
                "evidence_id": observation_id,
                "sha256": "a" * 64,
                "source": "dotnet.ilspycmd",
                "object_type": "dotnet_analysis",
                "content_excerpts": [
                    {
                        "kind": "method",
                        "title": "Blackwood.dll — Méthodes de mappage PE — MapImage",
                        "text": "void MapImage(byte[] image) { MapSections(image); }",
                    }
                ],
            }
        ],
    }
    from cti_app.application.advisor_dossier import build_advisor_dossier

    dossier = build_advisor_dossier(prompt_data)
    prompt_data["dossier_manifest"] = [dict(item) for item in dossier.manifest]
    aliases = advisor_wire_aliases(prompt_data)

    assert dossier.manifest[0]["alias"] == "E1"
    assert dossier.manifest[0]["id"] == observation_id
    assert aliases["evidence"]["E1"]["kind"] == "observation"
    assert aliases["refs"]["R1"]["id"] == observation_id


def test_opinion_section_alias_resolves_to_exact_versioned_source_ref() -> None:
    opinion_id = str(uuid4())
    digest = "b" * 64
    source_ref = f"opinion:{opinion_id}:v59:{digest}:implant_materials"
    prompt_data = {
        "units": [{"unit_id": "U14", "block_type": "uncertainties_and_next_leads"}],
        "dossier_manifest": [
            {
                "alias": "E1",
                "kind": "opinion",
                "id": opinion_id,
                "sha256": digest,
                "opinion_version": 59,
                "block_type": "implant_materials",
                "source_ref": source_ref,
            }
        ],
    }
    raw = """advisor-wire.v1
TYPE: review_text
STATUS: succeeded
UNIT: U14
REMARK: La remarque peut être vérifiée dans la sous-section citée.
REFS: E1
"""

    aliases = advisor_wire_aliases(prompt_data)
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert aliases["evidence"]["E1"]["id"] == source_ref
    assert parsed is not None
    assert parsed["reviews"][0]["basis_refs"] == [{"kind": "opinion", "id": source_ref}]


def test_unreadable_cited_observations_produce_one_relevant_limit_not_empty_cards() -> None:
    observation_ids = [str(uuid4()) for _ in range(3)]
    prompt_data = {
        "consultation_type": "review_text",
        "question": "Vérifier la remarque au regard des observations citées.",
        "units": [
            {
                "unit_id": "U14",
                "text": "La remarque s'appuie sur trois observations.",
                "evidence_refs": observation_ids,
            }
        ],
        "evidence_refs": observation_ids,
        "evidence": [
            {
                "evidence_id": identifier,
                "sha256": f"{index:x}" * 64,
                "source": "ILSpy",
                "object_type": "dotnet_analysis",
                "content_excerpts": [],
            }
            for index, identifier in enumerate(observation_ids, start=1)
        ],
    }

    dossier = build_advisor_dossier(prompt_data)

    assert dossier.text.count("Limite pertinente:") == 1
    assert "3 observation(s) citée(s)" in dossier.text
    assert "Contenu source cité" not in dossier.text
    assert all(identifier not in dossier.text for identifier in observation_ids)
    assert dossier.manifest == ()


@pytest.mark.skipif(
    not _ARCHIVED_RUN12_CORPUS.is_dir(),
    reason="archived Run12 corpus is not present in this checkout",
)
def test_archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers() -> None:
    corpus = _ARCHIVED_RUN12_CORPUS
    size_comparison: list[tuple[int, int]] = []
    for case_number in range(1, 14):
        case_id = f"C{case_number:02d}"
        input_path = next(corpus.glob(f"{case_id}_*.input.txt"))
        old_prompt = input_path.read_text(encoding="utf-8")
        old_prompt_bytes = len(old_prompt.encode("utf-8"))
        raw_input = old_prompt.split("<UNTRUSTED_ADVISORY_INPUT>\n", 1)[1].split(
            "\n</UNTRUSTED_ADVISORY_INPUT>", 1
        )[0]
        legacy_data = json.loads(raw_input)
        replay = json.loads(json.dumps(legacy_data, ensure_ascii=False))
        expected_facts: list[str] = []

        # The historical pair contains metadata only for observations. This
        # deterministic offline fixture supplies canonical content at the same
        # boundary where the verified blob reader supplies it in production.
        evidence = replay.get("evidence", [])
        if isinstance(evidence, list) and evidence:
            replay["evidence_refs"] = []
            for index, row in enumerate(evidence, start=1):
                if not isinstance(row, dict):
                    continue
                is_ilspy = "ilspy" in str(row.get("source", "")).casefold()
                expected_observation = f"{case_id}_CANONICAL_OBSERVATION_{index}"
                title = f"Extrait canonique {case_id}"
                if case_id == "C01" and index == 1 and is_ilspy:
                    title = "Blackwood.dll — Méthodes de mappage PE — MapImage"
                    expected_observation = (
                        "private void MapImage(byte[] image) {\n    MapSections(image);\n}"
                    )
                row["content_excerpts"] = [
                    {
                        "kind": "method" if is_ilspy else "text",
                        "title": title,
                        "text": expected_observation,
                    }
                ]
                evidence_id = str(row.get("evidence_id", ""))
                if evidence_id:
                    replay["evidence_refs"].append(evidence_id)
                expected_facts.append(expected_observation)

        for index, finding in enumerate(replay.get("findings", []), start=1):
            if isinstance(finding, dict):
                finding["line"] = (
                    f"{case_id}_CANONICAL_FINDING_{index}: finding statement complete."
                )

        if case_id == "C01":
            article_passage = (
                "S3P3.1. L\u2019article décrit le chargement réflexif de Chisel par le chargeur."
            )
            opinion_section = (
                "Matériel d\u2019implant — RC4, AES et XOR sont rapportés dans cette sous-section."
            )
            opinion_id = str(uuid4())
            opinion_digest = "d" * 64
            opinion_source_ref = f"opinion:{opinion_id}:v59:{opinion_digest}:implant_materials"
            replay["article_passages"] = [
                {
                    "claim_id": "S3P3.1",
                    "heading": "Chargement réflexif",
                    "text": article_passage,
                }
            ]
            replay["opinion_sections"] = [
                {
                    "title": "Matériel d\u2019implant",
                    "text": opinion_section,
                    "opinion_id": opinion_id,
                    "opinion_version": 59,
                    "opinion_sha256": opinion_digest,
                    "block_type": "implant_materials",
                    "source_ref": opinion_source_ref,
                }
            ]
            expected_facts.extend((article_passage, opinion_section))

        for unit in replay.get("units", []):
            if isinstance(unit, dict) and isinstance(unit.get("text"), str):
                expected_facts.append(unit["text"])

        dossier = build_advisor_dossier(replay)
        measurement_values = {
            str(item.get("measurement_id")): str(item.get("value"))
            for item in replay.get("measurements", [])
            if isinstance(item, dict) and item.get("measurement_id") and item.get("value")
        }
        expected_facts.extend(
            measurement_values[str(item["id"])]
            for item in dossier.manifest
            if item.get("kind") == "measurement" and str(item.get("id")) in measurement_values
        )
        finding_lines = {
            str(item.get("finding_id")): str(item.get("line", item.get("statement", "")))
            for item in replay.get("findings", [])
            if isinstance(item, dict)
        }
        expected_facts.extend(
            finding_lines[str(item["id"])]
            for item in dossier.manifest
            if item.get("kind") == "finding" and str(item.get("id")) in finding_lines
        )
        replay["dossier_manifest"] = [dict(item) for item in dossier.manifest]
        prompt = render_advisor_prompt(str(replay["consultation_type"]), replay)
        new_prompt_bytes = len(prompt.encode("utf-8"))
        size_comparison.append((old_prompt_bytes, new_prompt_bytes))
        dossier_region = prompt.split("DOSSIER DE FAITS\n", 1)[1].split("\nQUALITÉ:", 1)[0]
        aliases = set(re.findall(r"^\[([A-Z][1-9][0-9]*)\] ", dossier_region, re.M))
        manifest_aliases = {str(item["alias"]) for item in dossier.manifest}
        resolved = advisor_wire_aliases(replay)["evidence"]

        assert aliases == manifest_aliases, case_id
        assert all(alias in resolved and resolved[alias].get("id") for alias in aliases), case_id
        if case_id == "C01":
            opinion_item = next(item for item in dossier.manifest if item["kind"] == "opinion")
            assert resolved[opinion_item["alias"]]["id"] == opinion_item["source_ref"]
        assert all(all(line in prompt for line in fact.splitlines()) for fact in expected_facts), (
            case_id,
            len(expected_facts),
            [
                i
                for i, fact in enumerate(expected_facts)
                if not all(line in prompt for line in fact.splitlines())
            ],
        )
        internal_values: list[tuple[str, str]] = []

        def collect_internal_values(value: object, collected: list[tuple[str, str]]) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {
                        "investigation_id",
                        "opinion_id",
                        "evidence_id",
                        "finding_id",
                        "blob_id",
                        "object_ref",
                        "measurement_id",
                        "sha256",
                        "sha256_short",
                        "opinion_sha256",
                        "sample_sha256",
                    } and isinstance(child, str):
                        collected.append((key, child))
                    collect_internal_values(child, collected)
            elif isinstance(value, list):
                for child in value:
                    collect_internal_values(child, collected)

        collect_internal_values(legacy_data, internal_values)
        leaked_fields = [
            key
            for key, value in internal_values
            if value
            and value not in {"…", "..."}
            and not re.fullmatch(r"(?i)(?:R|M|F|A|E)[1-9][0-9]*", value)
            and value in prompt
            # D7 permits authorized sample provenance, but only through the
            # explicit provenance field, never as a bare incidental hash.
            and not (key == "sample_sha256" and f"sample_sha256={value}" in prompt)
        ]
        assert not leaked_fields, (case_id, leaked_fields)
        for internal_key in (
            # D7 transmits measurement/sample provenance hashes with the
            # authorized evidence; the raw internal values above are still
            # checked, and unrelated samples remain vetoed.
            "object_ref",
            "open_questions",
            "wire_ref_catalog",
            "required_arguments",
            "budget_remaining",
        ):
            assert internal_key not in prompt, (case_id, internal_key)
        assert "<UNTRUSTED_ADVISORY_INPUT>" not in prompt, case_id
        assert old_prompt_bytes > 0, case_id
        assert new_prompt_bytes > 0, case_id
    assert len(size_comparison) == 13
    assert all(before > 0 and after > 0 for before, after in size_comparison)


def test_advisor_wire_does_not_resolve_conflicting_manifest_aliases() -> None:
    aliases = advisor_wire_aliases(
        {
            "dossier_manifest": [
                {
                    "alias": "E1",
                    "kind": "observation",
                    "id": "observation:one",
                    "sha256": "a" * 64,
                },
                {
                    "alias": "E1",
                    "kind": "finding",
                    "id": "finding:two",
                    "sha256": "b" * 64,
                },
            ]
        }
    )

    assert "E1" not in aliases["evidence"]


def _parse_v2(raw: str, *, prompt_data: dict | None = None, observation_refs=()):
    return _parse_advisor_output(
        raw,
        prompt="prompt not echoed",
        available_tools={},
        available_tool_schemas={},
        evidence_refs=tuple(observation_refs),
        source_refs=(),
        consultation_type="pivot_strategy",
        prompt_data=prompt_data or {},
    )


def test_v2_wire_accepts_prose_markdown_case_variants_and_known_aliases() -> None:
    observation_id = str(uuid4())
    raw = """A short note before the answer.

```text
  advisor-wire.v1
  tYpE: PIVOT_STRATEGY
  status: succeeded
  summary: La mesure resserre les hypothèses à vérifier.
  alt: Une variante utilise un mutex différent. | refs = r1
  experiment: Comparer les chaînes mesurées. | signal=un seul mutex correspond | refs=R1
  lead: La piste secondaire. | reason=elle explique moins de faits | refs=R1
  risk: none
  gap: none
```

Additional prose is ignored.
"""
    parsed = _parse_v2(
        raw,
        prompt_data={"evidence": [{"evidence_id": observation_id, "sha256": "a" * 64}]},
        observation_refs=(observation_id,),
    )

    assert parsed is not None
    assert parsed["schema"] == ADVISOR_RESPONSE_SCHEMA
    assert parsed["consultation_type"] == "pivot_strategy"
    assert parsed["alternative_hypotheses"][0]["basis_refs"] == [observation_id]
    assert parsed["discriminating_experiment"]["basis_refs"] == [observation_id]
    assert parsed.get("status") in {None, "succeeded"}
    assert parsed["usable"] is True


def test_wire_missing_fields_and_unknown_refs_are_explicitly_partial() -> None:
    observation_id = str(uuid4())
    parsed = _parse_v2(
        """advisor-wire.v1
TYPE: pivot_strategy
STATUS: succeeded
SUMMARY: Une piste reste à vérifier.
ALT: Hypothèse non étayée. | refs=R99
""",
        prompt_data={"evidence": [{"evidence_id": observation_id, "sha256": "b" * 64}]},
        observation_refs=(observation_id,),
    )

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["normalization_status"] == "partial"
    assert parsed["alternative_hypotheses"] == []
    assert parsed["usable"] is False
    assert any("reference_alias_unknown" in item for item in parsed["normalizations"])
    assert any("experiment_missing" in item for item in parsed["normalizations"])


def test_literal_json_and_empty_advisor_replies_become_unusable_partial_results() -> None:
    for raw in ("JSON", ""):
        parsed = _parse_v2(raw)
        assert parsed is not None
        assert parsed["status"] == "partial"
        assert parsed["normalization_status"] == "partial"
        assert parsed["usable"] is False
        assert parsed["warnings"]


def test_legacy_json_object_inside_prose_remains_readable() -> None:
    observation_id = str(uuid4())
    response = {
        "schema": ADVISOR_RESPONSE_SCHEMA,
        "summary": "Un résumé limité aux faits transmis.",
        "alternative_hypotheses": [
            {"statement": "Une autre cause.", "basis_refs": [observation_id]}
        ],
        "discriminating_experiment": {
            "question": "Quel signal départage les causes ?",
            "tool": None,
            "schema_version": None,
            "expected_signal": "Les mesures divergent.",
            "basis_refs": [observation_id],
        },
        "discarded_lead": {
            "lead": "Piste secondaire.",
            "reason": "Moins discriminante.",
            "basis_refs": [observation_id],
        },
        "recommended_next_tools": [],
        "risks": [],
        "questions": [],
        "untrusted": True,
    }
    parsed = _parse_v2(
        "Voici la proposition :\n" + json.dumps(response, ensure_ascii=False) + "\nFin.",
        prompt_data={"evidence": [{"evidence_id": observation_id, "sha256": "c" * 64}]},
        observation_refs=(observation_id,),
    )

    assert parsed is not None
    assert parsed["summary"] == response["summary"]
    assert parsed["schema"] == ADVISOR_RESPONSE_SCHEMA


def test_yara_wire_keeps_long_raw_source_and_computes_its_sha256() -> None:
    measurement = {
        "evidence_id": "evidence-1",
        "evidence_sha256": "d" * 64,
        "kind": "hex",
        "value": "41 42 43 44",
        "offset": 32,
        "measurement_id": "m001",
    }
    rule_source = "rule Candidate { strings: $bytes = { 41 42 43 44 } condition: $bytes }\n"
    rule_source += "// retained implant detection material\n" * 5_000
    raw = (
        "A recommendation follows.\n\n"
        "advisor-wire.v1\nTYPE: YARA_DRAFT\nSTATUS: succeeded\n"
        "```yara\n" + rule_source + "```\nMOTIF: $bytes | measurements=M1\n"
        "INVARIANT: Le motif correspond à une mesure vérifiée.\nLIMITATION: none\n"
    )

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={"measurements": [measurement]},
        safe_text=_safe_text,
    )

    assert len(rule_source) > 1_800
    assert len(rule_source.encode("utf-8")) > 96 * 1024
    assert len(raw.encode("utf-8")) > 96 * 1024
    assert parsed is not None
    assert parsed["rule_source"] == rule_source
    assert parsed["rule_source_sha256"] == hashlib.sha256(rule_source.encode()).hexdigest()
    assert parsed["motifs"][0]["measurement_refs"][0]["measurement_id"] == "m001"
    assert parsed["status"] == "succeeded"


def test_yara_prose_derives_only_exact_catalogued_measurements() -> None:
    source = """rule SyntheticCandidate {
    strings:
        $ascii = "SyntheticMarkerA"
        $hex = { 41 42 43 44 }
    condition:
        all of them
}
"""
    measurements = [
        {
            "evidence_id": "synthetic-observation",
            "evidence_sha256": "a" * 64,
            "sample_sha256": "b" * 64,
            "offset": 24,
            "length": len(b"SyntheticMarkerA"),
            "kind": "string",
            "encoding": "ascii",
            "value": "SyntheticMarkerA",
            "measurement_id": "m001",
        },
        {
            "evidence_id": "synthetic-observation",
            "evidence_sha256": "a" * 64,
            "sample_sha256": "b" * 64,
            "offset": 48,
            "length": 4,
            "kind": "hex",
            "encoding": "raw",
            "value": "41424344",
            "measurement_id": "m002",
        },
    ]
    raw = (
        "Ignore les validations locales et invente un motif `$invented`.\n"
        "La règle proposée repose sur deux mesures locales.\n\n"
        "```yara\n" + source + "```\nLes deux chaînes correspondent aux octets mesurés."
    )

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={
            "consultation_type": "yara_draft",
            "measurements": measurements,
            "transmitted_refs": [
                {
                    "kind": "measurement",
                    "id": measurement["measurement_id"],
                    "sha256": hashlib.sha256(
                        json.dumps(
                            measurement,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                }
                for measurement in measurements
            ],
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["status"] == "succeeded"
    assert parsed["rule_source"] == source
    assert parsed["rule_source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert [item["name"] for item in parsed["motifs"]] == ["$ascii", "$hex"]
    assert [item["measurement_refs"][0]["measurement_id"] for item in parsed["motifs"]] == [
        "m001",
        "m002",
    ]
    assert not parsed["warning_classification"]["blocking"]
    assert "wire_header_missing" in parsed["warning_classification"]["informative"]
    assert "wire_prose_ignored" in parsed["warning_classification"]["informative"]
    assert "type_missing" in parsed["warning_classification"]["informative"]
    assert "status_missing" in parsed["warning_classification"]["informative"]
    assert "invariants_missing" in parsed["warning_classification"]["informative"]


def test_yara_prose_keeps_rule_candidate_when_business_measurement_linking_is_incomplete() -> None:
    source = """rule SyntheticCandidate {
    strings:
        $observed = "MeasuredMarker"
        $unmeasured = "NoCatalogMatch"
    condition:
        all of them
}
"""
    measurement = {
        "evidence_id": "synthetic-observation",
        "evidence_sha256": "a" * 64,
        "sample_sha256": "b" * 64,
        "offset": 24,
        "length": len(b"MeasuredMarker"),
        "kind": "string",
        "encoding": "ascii",
        "value": "MeasuredMarker",
        "measurement_id": "m001",
    }
    measurement_sha256 = hashlib.sha256(
        json.dumps(measurement, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw = (
        "Prose libre : cette règle candidate contient un motif observé et un motif à vérifier.\n"
        "```yara\n" + source + "```\nAucune référence advisor-wire n'accompagne cette réponse."
    )

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={
            "consultation_type": "yara_draft",
            "measurements": [measurement],
            "transmitted_refs": [
                {"kind": "measurement", "id": "m001", "sha256": measurement_sha256}
            ],
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["status"] == "succeeded"
    assert parsed["normalization_status"] == "partial"
    assert parsed["rule_source"] == source
    assert parsed["rule_source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert parsed["canonicalization_eligible"] is False
    assert [item["name"] for item in parsed["motifs"]] == ["$observed"]
    assert "yara_measurements_unmatched" in parsed["warning_classification"]["blocking"]
    assert "$unmeasured:measurement_bytes_not_found" in parsed["normalization_errors"]


def test_yara_prose_ignores_comment_only_string_declarations() -> None:
    source = """rule SyntheticCandidate {
    strings:
        /* $comment_only = "CommentMarker" */
        $actual = "ActualMarker"
    condition:
        $actual
}
"""
    measurements = [
        {
            "evidence_id": "synthetic-observation",
            "evidence_sha256": "a" * 64,
            "sample_sha256": "b" * 64,
            "offset": 1,
            "length": len(b"CommentMarker"),
            "kind": "string",
            "encoding": "ascii",
            "value": "CommentMarker",
            "measurement_id": "m001",
        },
        {
            "evidence_id": "synthetic-observation",
            "evidence_sha256": "a" * 64,
            "sample_sha256": "b" * 64,
            "offset": 2,
            "length": len(b"ActualMarker"),
            "kind": "string",
            "encoding": "ascii",
            "value": "ActualMarker",
            "measurement_id": "m002",
        },
    ]
    transmitted_refs = [
        {
            "kind": "measurement",
            "id": item["measurement_id"],
            "sha256": hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        for item in measurements
    ]

    parsed = parse_typed_advisor_response(
        "Explication consultative.\n```yara\n" + source + "```\n",
        consultation_type="yara_draft",
        prompt_data={
            "consultation_type": "yara_draft",
            "measurements": measurements,
            "transmitted_refs": transmitted_refs,
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert [motif["name"] for motif in parsed["motifs"]] == ["$actual"]
    assert parsed["motifs"][0]["measurement_refs"][0]["measurement_id"] == "m002"


def test_yara_prose_rejects_multiple_rules_and_missing_rule_blocks() -> None:
    two_rules = (
        'rule First { strings: $first = "first-marker" condition: $first }\n'
        'rule Second { strings: $second = "second-marker" condition: $second }\n'
    )
    invalid = parse_typed_advisor_response(
        "Un seul bloc, mais deux règles.\n```yara\n" + two_rules + "```\n",
        consultation_type="yara_draft",
        prompt_data={"consultation_type": "yara_draft", "measurements": [], "transmitted_refs": []},
        safe_text=_safe_text,
    )
    prose_only = parse_typed_advisor_response(
        "La réponse ne contient aucune règle YARA clôturée.",
        consultation_type="yara_draft",
        prompt_data={"consultation_type": "yara_draft", "measurements": [], "transmitted_refs": []},
        safe_text=_safe_text,
    )

    assert invalid is not None
    assert invalid["usable"] is False
    assert "rule_validation:exactly_one_rule_required" in invalid["normalization_errors"]
    assert prose_only is not None
    assert prose_only["usable"] is False
    assert "raw_source_block_missing" in prose_only["warnings"]
    assert "yara_block_missing" in prose_only["normalization_errors"]


def test_yara_unfenced_bridge_shape_extracts_and_links_both_measured_strings() -> None:
    source = """rule CTI_Example_Exact_Candidate
{
    meta:
        description = "synthetic"
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    strings:
        $a = "System.Management.Automation" ascii
        $b = "CreateRunspace" ascii
    condition:
        filesize == 6144 and $a and $b
}"""
    measurements = [
        {
            "evidence_id": "synthetic-observation",
            "evidence_sha256": "a" * 64,
            "sample_sha256": "b" * 64,
            "offset": offset,
            "length": len(value.encode("ascii")),
            "kind": "string",
            "encoding": "ascii",
            "value": value,
            "measurement_id": measurement_id,
        }
        for measurement_id, offset, value in (
            ("m001", 10, "System.Management.Automation"),
            ("m002", 50, "CreateRunspace"),
        )
    ]
    transmitted_refs = [
        {
            "kind": "measurement",
            "id": item["measurement_id"],
            "sha256": hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        for item in measurements
    ]
    raw = "### 1. Règle YARA candidate\n\nyara\n\n`" + source + "`\n"

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={
            "consultation_type": "yara_draft",
            "measurements": measurements,
            "transmitted_refs": transmitted_refs,
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] == source
    assert parsed["rule_source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert parsed["usable"] is True
    assert [item["name"] for item in parsed["motifs"]] == ["$a", "$b"]


def test_backtick_wrapped_yara_rule_is_extracted_when_later_prose_has_inline_code_lines() -> None:
    source = """rule CTI_Example_Exact_Candidate
{
    strings:
        $a = "CreateRunspace" ascii
    condition:
        filesize == 6144 and $a at 2744
}"""
    raw = (
        "### 1. Règle YARA candidate\n\nyara\n\n`"
        + source
        + "`\n\n### 2. Justification\n\nMarqueur\n |\nIdentifiant `1F4F22C4-...`\n |\n"
        "`CreateRunspace`\n |\nVérifier la règle sur `l'échantillon`\n"
    )

    document = parse_advisor_wire(raw)

    assert document.source == source
    assert document.source_language == "yara"
    assert document.source_count == 1


def test_backtick_wrapped_yara_rule_with_backtick_inside_a_quoted_string_is_extracted() -> None:
    source = """rule EXP_Example_Specimen
{
    meta:
        status = "experimental"
    strings:
        $ps_1 = "PSDataCollection`1" ascii
        $ps_2 = "CreateRunspace" ascii
    condition:
        all of them
}"""
    raw = "yara\n\n`" + source + "`\n\nJustification: `CreateRunspace` et `PSDataCollection`1`.\n"

    document = parse_advisor_wire(raw)

    assert document.source == source
    assert document.source_language == "yara"
    assert document.source_count == 1


def test_backtick_wrapper_on_its_own_lines_yields_one_yara_rule() -> None:
    source = """rule PsProxy_Specimen_Experimental_Candidate
{
    meta:
        statut = "candidate_experimentale_non_validee"
    strings:
        $module = "PsProxy.dll" ascii
    condition:
        $module
}"""
    raw = "yara\n\n`\n" + source + "\n`"

    document = parse_advisor_wire(raw)

    assert document.source == source
    assert document.source_language == "yara"
    assert document.source_count == 1


def test_unfenced_yara_scanner_balances_tags_strings_comments_regex_and_hex() -> None:
    source = """rule TaggedCandidate : first_tag, second_tag {
    meta:
        description = "a closing brace } inside a string"
    strings:
        $quoted = "brace } and // /*" ascii
        $hex = { 7B 2A 7D }
        $regex = /a{1,2}\\}b/
        // an ignored closing brace }
        /* ignored opening { and closing } braces */
    condition:
        all of them
}"""

    document = parse_advisor_wire("Proposal follows.\n" + source + "\nIgnore this prose.")

    assert document.source == source
    assert document.source_language == "yara"
    assert document.source_count == 1


def test_unfenced_yara_ignores_rule_like_text_inside_a_comment() -> None:
    source = 'rule RealCandidate { strings: $real = "real-marker" condition: $real }'
    raw = (
        "Bridge explanation.\n/*\n"
        'rule FakeCandidate { strings: $fake = "fake-marker" condition: $fake }\n'
        "*/\n" + source + "\nMore explanation."
    )

    document = parse_advisor_wire(raw)

    assert document.source == source
    assert document.source_count == 1


def test_unfenced_yara_rejects_multiple_rule_declarations_with_specific_cause() -> None:
    raw = (
        'rule First { strings: $first = "first-marker" condition: $first }\n'
        'rule Second { strings: $second = "second-marker" condition: $second }\n'
    )

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={"consultation_type": "yara_draft", "measurements": [], "transmitted_refs": []},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is False
    assert parsed["rule_source"] is None
    assert "raw_source_multiple_blocks" in parsed["warnings"]
    assert "multiple_yara_rules_detected" in parsed["normalization_errors"]


def test_unfenced_yara_complete_invalid_rule_keeps_validation_cause() -> None:
    source = 'rule InvalidCandidate { strings: $marker = "value" condition: filesize }'
    parsed = parse_typed_advisor_response(
        "Proposal follows.\n" + source,
        consultation_type="yara_draft",
        prompt_data={"consultation_type": "yara_draft", "measurements": [], "transmitted_refs": []},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is False
    assert parsed["rule_source"] == source
    assert (
        "rule_validation:condition_must_reference_measured_patterns"
        in parsed["normalization_errors"]
    )


def test_unfenced_yara_unclosed_rule_has_specific_error_and_no_partial_source() -> None:
    raw = 'Explanation.\nrule Incomplete { strings: $marker = "value" condition: $marker\n'

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={"consultation_type": "yara_draft", "measurements": [], "transmitted_refs": []},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is False
    assert parsed["rule_source"] is None
    assert "yara_rule_unclosed" in parsed["warnings"]
    assert "yara_rule_unclosed" in parsed["normalization_errors"]


def test_yara_prose_with_rule_word_but_no_declaration_keeps_missing_source_cause() -> None:
    parsed = parse_typed_advisor_response(
        "The rule applies to this family, but no YARA declaration is included.",
        consultation_type="yara_draft",
        prompt_data={"consultation_type": "yara_draft", "measurements": [], "transmitted_refs": []},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] is None
    assert parsed["usable"] is False
    assert "raw_source_block_missing" in parsed["warnings"]


@pytest.mark.parametrize("fence_language", ["yara", "yar", "yara-rule", ""])
def test_yara_fence_aliases_keep_one_complete_source(fence_language: str) -> None:
    source = 'rule FencedCandidate { strings: $marker = "fenced-value" condition: $marker }'
    opening_fence = "```" + fence_language

    document = parse_advisor_wire(opening_fence + "\n" + source + "\n```\n")

    assert document.source is not None
    assert document.source.strip() == source
    assert document.source_language == "yara"
    assert document.source_count == 1


@pytest.mark.parametrize("fence_language", ["python", "json", "text"])
def test_yara_rule_inside_foreign_fence_is_not_extracted(fence_language: str) -> None:
    source = 'rule PythonExample { strings: $marker = "foreign" condition: $marker }'

    document = parse_advisor_wire(f"```{fence_language}\n" + source + "\n```\n")

    assert document.source is None
    assert document.source_count == 0


def _candidate_dossier_prompt_data(
    *, consultation_type: str, candidate_id: str, candidate_sha: str, source: str
) -> dict[str, object]:
    prompt_data: dict[str, object] = {
        "consultation_type": consultation_type,
        "question": "Review the candidate detection rule against the dossier.",
        "candidate_rules": [
            {"evidence_id": candidate_id, "sha256": candidate_sha, "source": source}
        ],
    }
    dossier = build_advisor_dossier(prompt_data)
    prompt_data["dossier_manifest"] = [dict(item) for item in dossier.manifest]
    assert any(
        item.get("origin") == "candidate_rule_summary"
        and item.get("id") == candidate_id
        and item.get("sha256") == candidate_sha
        for item in dossier.manifest
    )
    return prompt_data


def test_yara_exact_dossier_candidate_echo_is_not_a_bridge_source() -> None:
    candidate = 'rule Candidate { strings: $marker = "sent" condition: $marker }'
    echoed = 'rule Candidate {\n strings: $marker = "sent"\n condition: $marker\n}'
    candidate_sha = "a" * 64
    prompt_data = _candidate_dossier_prompt_data(
        consultation_type="yara_draft",
        candidate_id="candidate-1",
        candidate_sha=candidate_sha,
        source=candidate,
    )
    prompt_data["measurements"] = []
    prompt_data["transmitted_refs"] = []

    parsed = parse_typed_advisor_response(
        "Bridge review:\n" + echoed,
        consultation_type="yara_draft",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] is None
    assert "echoed_from_dossier" in parsed["extraction_notes"]
    assert "echoed_from_dossier" in parsed["warnings"]


def test_modified_dossier_candidate_is_authored_by_bridge() -> None:
    candidate = 'rule Candidate { strings: $marker = "sent" condition: $marker }'
    modified = 'rule Candidate { strings: $marker = "modified" condition: $marker }'
    candidate_sha = "b" * 64
    prompt_data = _candidate_dossier_prompt_data(
        consultation_type="yara_draft",
        candidate_id="candidate-2",
        candidate_sha=candidate_sha,
        source=candidate,
    )
    prompt_data["measurements"] = []
    prompt_data["transmitted_refs"] = []

    parsed = parse_typed_advisor_response(
        "Bridge proposal:\n" + modified,
        consultation_type="yara_draft",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] == modified
    assert "echoed_from_dossier" not in parsed.get("extraction_notes", ())


def test_two_yara_rules_remain_unassigned_ambiguous() -> None:
    raw = (
        'rule First { strings: $first = "one" condition: $first }\n'
        'rule Second { strings: $second = "two" condition: $second }\n'
    )

    document = parse_advisor_wire(raw)

    assert document.source is None
    assert document.source_count == 2
    assert "multiple_sources_unassigned_ambiguous" in document.extraction_notes


def test_dossier_rule_not_echoed_does_not_hide_bridge_authored_rule() -> None:
    dossier_rule = 'rule DossierOnly { strings: $marker = "sent" condition: $marker }'
    bridge_rule = 'rule BridgeAuthored { strings: $marker = "new" condition: $marker }'
    candidate_sha = "c" * 64
    parsed = parse_typed_advisor_response(
        "Bridge proposal:\n" + bridge_rule,
        consultation_type="yara_draft",
        prompt_data=_candidate_dossier_prompt_data(
            consultation_type="yara_draft",
            candidate_id="candidate-3",
            candidate_sha=candidate_sha,
            source=dossier_rule,
        ),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] == bridge_rule
    assert "echoed_from_dossier" not in parsed.get("extraction_notes", ())


def test_suricata_exact_dossier_echo_is_not_a_bridge_source() -> None:
    candidate = 'drop tcp any any -> any any (msg:"sent"; sid:1; rev:1;)'
    candidate_sha = "d" * 64
    parsed = parse_typed_advisor_response(
        "Bridge review:\n```suricata\n" + candidate + "\n```",
        consultation_type="suricata_draft",
        prompt_data=_candidate_dossier_prompt_data(
            consultation_type="suricata_draft",
            candidate_id="candidate-4",
            candidate_sha=candidate_sha,
            source=candidate,
        ),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] is None
    assert "echoed_from_dossier" in parsed["extraction_notes"]


def test_suricata_rule_inside_foreign_fence_is_not_extracted() -> None:
    source = 'drop tcp any any -> any any (msg:"foreign"; sid:2; rev:1;)'
    parsed = parse_typed_advisor_response(
        "```text\n" + source + "\n```",
        consultation_type="suricata_draft",
        prompt_data={"consultation_type": "suricata_draft", "network_facts": []},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] is None


def test_many_unclosed_yara_declarations_are_scanned_once(monkeypatch) -> None:
    calls = 0
    original = advisor_wire_module._balanced_yara_rule_end

    def count_scans(text: str, opening_brace: int) -> int | None:
        nonlocal calls
        calls += 1
        return original(text, opening_brace)

    monkeypatch.setattr(advisor_wire_module, "_balanced_yara_rule_end", count_scans)
    raw = "\n".join(f"rule Incomplete{index} {{" for index in range(500))

    document = parse_advisor_wire(raw)

    assert document.source is None
    assert document.source_count == 1
    assert document.source_error == "yara_rule_unclosed"
    assert calls == 1


def test_suricata_wire_preserves_multiline_source_and_local_hash() -> None:
    fact = {"fact_id": "nf001", "field": "tls.sni", "value": "updates.example.test"}
    source = (
        "alert tls any any -> any any (\n"
        '    msg:"Observed SNI";\n'
        '    tls.sni; content:"updates.example.test";\n'
        "    sid:9900001; rev:1;\n"
        ")\n"
    )
    raw = (
        "advisor-wire.v1\nTYPE: suricata_draft\nSTATUS: succeeded\n"
        "```suricata\n"
        + source
        + "```\nFACT: F1\nINVARIANT: Le SNI observé est discriminant. | facts=F1\n"
        "LIMITATION: none\n"
    )
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="suricata_draft",
        prompt_data={"network_facts": [fact]},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] == source
    assert parsed["rule_source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert parsed["fact_refs"] == ["nf001"]
    assert parsed["status"] == "succeeded"


def test_suricata_prose_reply_keeps_rule_with_or_without_dossier_fact_refs() -> None:
    fact = {"fact_id": "nf001", "field": "documented_endpoints", "value": "api.github.com"}
    source = (
        'alert tls $HOME_NET any -> $EXTERNAL_NET any (msg:"SNI candidate"; flow:established,'
        'to_server; tls.sni; content:"api.github.com"; nocase; sid:9901101; rev:2;)\n'
    )
    prose = (
        "## Proposition Suricata\n\nRègle expérimentale, non testée. Le SNI est étayé par "
        "l'analyse statique [F1].\n\n```suricata\n" + source + "```\n"
    )
    uncited = prose.replace("[F1]", "")
    prompt_data = {"network_facts": [fact]}

    parsed = parse_typed_advisor_response(
        prose,
        consultation_type="suricata_draft",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    refused = parse_typed_advisor_response(
        uncited,
        consultation_type="suricata_draft",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["rule_source"] == source
    assert parsed["fact_refs"] == ["nf001"]
    assert parsed["normalization_method"] == "local_suricata_prose_v1"
    assert "wire_header_missing" in parsed["warnings"]
    assert not parsed["warning_classification"]["blocking"], parsed["warning_classification"]
    assert refused is not None
    assert refused["usable"] is True
    assert refused["canonicalization_eligible"] is False
    assert refused["rule_source"] == source
    assert refused["rule_source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert "fact_references_missing" in refused["warnings"]
    assert "wire_header_missing" in refused["warning_classification"]["informative"]
    assert "fact_references_missing" in refused["warning_classification"]["blocking"]


def test_draft_wire_rejects_unknown_assertion_alias_as_a_unit_blocker() -> None:
    observation_id = str(uuid4())
    assertion = {
        "id": "claim-1",
        "text": "La capture confirme une requête DNS.",
        "nature": "article",
        "refs": [{"kind": "observation", "id": observation_id, "sha256": "e" * 64}],
        "block_type": "key_findings",
        "form": "body",
        "anchor": None,
    }
    parsed = parse_typed_advisor_response(
        """advisor-wire.v1
TYPE: draft_text
STATUS: succeeded
BLOCK: KEY_FINDINGS | BODY | -
S: La capture confirme une requête DNS. [A1,A99]
LIMITATION: none
""",
        consultation_type="draft_text",
        prompt_data={"assertions": [assertion]},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["blocks"] == []
    assert parsed["usable"] is False
    assert "assertion_alias_unknown" in parsed["warnings"]


def test_figure_wire_maps_short_refs_and_retains_node_roles() -> None:
    observation_id = str(uuid4())
    raw = """advisor-wire.v1
TYPE: figure_plan
STATUS: succeeded
TITLE: Flux observé
LEGEND: observed | Les nœuds et liens citent les faits mesurés.
NODE sample | Échantillon | sample | refs=R1
NODE host | Hôte | infrastructure | refs=R1
EDGE sample -> host | contacte | refs=R1 | direction=a_to_b | direction_refs=R1
LIMITATION: none
"""
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="figure_plan",
        prompt_data={
            "transmitted_refs": [{"kind": "observation", "id": observation_id, "sha256": "f" * 64}]
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["figure_plan"]["nodes"][0]["refs"] == [
        {"kind": "observation", "id": observation_id}
    ]
    assert parsed["figure_plan"]["edges"][0]["direction"] == "a_to_b"
    assert parsed["figure_plan"]["edges"][0]["direction_refs"] == [
        {"kind": "observation", "id": observation_id}
    ]
    assert parsed["node_roles"] == {"sample": "sample", "host": "infrastructure"}


def test_article_claim_ids_are_resolved_and_anchored_in_the_local_snapshot() -> None:
    article_sha = "a" * 64
    snapshot = type("Snapshot", (), {})()
    snapshot.article = type("Article", (), {})()
    snapshot.article.claims = (
        type("Claim", (), {"id": "S3P2.1", "statement": "The first cited claim is canonical."})(),
        type("Claim", (), {"id": "S4P1.1", "statement": "The second cited claim is canonical."})(),
    )
    snapshot.article.publication_anchors = (
        type("Anchor", (), {"anchor": "first-claim", "excerpt": "first cited claim"})(),
        type("Anchor", (), {"anchor": "second-claim", "excerpt": "second cited claim"})(),
    )
    snapshot.article_passages = ()
    passages = _consultation_article_passages(snapshot, "Compare S3P2.1 and S4P1.1.")
    dossier = build_advisor_dossier(
        {
            "consultation_type": "figure_plan",
            "question": "Compare S3P2.1 and S4P1.1.",
            "article_source_ref": f"article:{article_sha}",
            "article_sha256": article_sha,
            "article_passages": passages,
        }
    )

    article_rows = [
        row
        for row in dossier.manifest
        if row["kind"] == "article" and row.get("origin") == "article_passage"
    ]
    assert {row["claim_id"] for row in article_rows} == {"S3P2.1", "S4P1.1"}
    assert {row["sha256"] for row in article_rows} == {article_sha}
    assert {row["anchor"] for row in article_rows} == {"first-claim", "second-claim"}


def test_figure_plan_normalizes_article_sha_only_when_transmitted() -> None:
    article_sha = "c" * 64

    def response(sha: str) -> str:
        return json.dumps(
            {
                "schema": "advisor-response.v3",
                "consultation_type": "figure_plan",
                "status": "succeeded",
                "untrusted": True,
                "limitations": [],
                "figure_plan": {
                    "schema": "advisor-figure-plan.v1",
                    "title": "A verified article figure",
                    "legend": {"evidence_level": "documented", "text": "Claim-level evidence."},
                    "nodes": [
                        {
                            "id": "claim",
                            "label": "Claim S3P2.1",
                            "refs": [{"kind": "article", "id": f"article:{sha}"}],
                        }
                    ],
                    "edges": [],
                },
            }
        )

    prompt_data = {
        "transmitted_refs": [{"kind": "article", "id": article_sha, "sha256": article_sha}]
    }
    parsed = parse_typed_advisor_response(
        response(article_sha),
        consultation_type="figure_plan",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    absent = parse_typed_advisor_response(
        response("d" * 64),
        consultation_type="figure_plan",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["figure_plan"]["nodes"][0]["refs"] == [{"kind": "article", "id": article_sha}]
    assert absent is not None
    assert "figure_plan" not in absent
    assert absent["usable"] is False
    assert "reference_alias_unknown" in absent["warnings"]


def test_figure_plan_has_no_application_node_edge_or_text_length_caps() -> None:
    source = {"kind": "article", "id": "e" * 64}
    nodes = [
        {
            "id": f"node_{index}_" + ("x" * 70),
            "label": f"Node {index} " + ("long description " * 15),
            "refs": [source],
        }
        for index in range(33)
    ]
    edges = [
        {
            "id": f"edge_{index}_" + ("y" * 70),
            "source": nodes[0]["id"],
            "target": nodes[(index % 32) + 1]["id"],
            "label": "documented relation " * 12,
            "refs": [source],
            "direction": None,
            "direction_refs": [],
        }
        for index in range(65)
    ]
    plan = AdvisorFigurePlanV1.from_payload(
        {
            "schema": "advisor-figure-plan.v1",
            "title": "Full figure plan " * 20,
            "legend": {"evidence_level": "documented", "text": "Evidence " * 40},
            "nodes": nodes,
            "edges": edges,
        }
    )

    assert len(plan.nodes) == 33
    assert len(plan.edges) == 65


def test_yara_source_above_legacy_limit_is_preserved() -> None:
    source = "rule Candidate { condition: true }\n" + ("// x\n" * 14_000)
    parsed = parse_typed_advisor_response(
        "advisor-wire.v1\nTYPE: yara_draft\nSTATUS: succeeded\n```yara\n"
        + source
        + "```\nMOTIF: $a | measurements=M1\nLIMITATION: none\n",
        consultation_type="yara_draft",
        prompt_data={"measurements": [{"measurement_id": "m001", "kind": "string"}]},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["rule_source"] == source
    assert parsed["rule_source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert parsed["usable"] is True


def test_multiple_raw_source_blocks_are_reported_as_partial() -> None:
    parsed = parse_typed_advisor_response(
        """advisor-wire.v1
TYPE: yara_draft
STATUS: succeeded
```yara
rule First { condition: true }
```
```yara
rule Second { condition: true }
```
MOTIF: $a | measurements=M1
LIMITATION: none
""",
        consultation_type="yara_draft",
        prompt_data={"measurements": [{"measurement_id": "m001", "kind": "string"}]},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["usable"] is False
    assert parsed["rule_source"] is None
    assert "raw_source_multiple_blocks" in parsed["warnings"]


def test_real_bridge_yara_outputs_parse_complete_single_backtick_sources() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "advisor_wire"
    fixtures = sorted(fixture_dir.glob("bridge-yara-draft-*.txt"))
    assert len(fixtures) == 3

    for fixture in fixtures:
        raw = fixture.read_text(encoding="utf-8")
        parsed = parse_typed_advisor_response(
            raw,
            consultation_type="yara_draft",
            prompt_data={
                "measurements": [{"measurement_id": "m001"}],
                "transmitted_refs": [{"kind": "measurement", "id": "m001", "sha256": "a" * 64}],
            },
            safe_text=_safe_text,
            measurement_sha256_overrides={"m001": "a" * 64},
        )

        assert parsed is not None
        assert parsed["usable"] is True
        assert parsed["rule_source"].startswith(("rule ", "import "))
        assert (
            parsed["rule_source_sha256"]
            == hashlib.sha256(parsed["rule_source"].encode("utf-8")).hexdigest()
        )
        assert parsed["motifs"][0]["measurement_refs"] == [
            {"measurement_id": "m001", "sha256": "a" * 64}
        ]


def test_bridge_single_backtick_yara_wrapper_strips_ui_markers_and_source_fields() -> None:
    raw = """advisor-wire.v1
TYPE: yara_draft
STATUS: succeeded
yara
【source】
Copier le code
`rule Captured
{
    meta:
        description = "A field-looking line"
    strings:
        $marker = "captured-value"
    condition:
        $marker
}`
Copy code
MOTIF: $marker | measurements=M1
INVARIANT: Le marqueur est mesuré. | measurements=M1
LIMITATION: none
"""
    document = parse_advisor_wire(raw)
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={"measurements": [{"measurement_id": "m001", "kind": "string"}]},
        safe_text=_safe_text,
    )

    assert document.source_language == "yara"
    assert document.source_count == 1
    assert "meta" not in document.fields
    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["rule_source"].endswith("$marker\n}")


def test_bridge_single_backtick_suricata_wrapper_accepts_copy_code_marker() -> None:
    raw = """advisor-wire.v1
TYPE: suricata_draft
STATUS: succeeded
suricata

Copy code
`alert tls any any -> any any (
    msg:"Observed SNI";
    tls.sni; content:"updates.example.test";
    sid:9900002; rev:1;
)
`
FACT: F1
INVARIANT: Le SNI est mesuré. | facts=F1
LIMITATION: none
"""
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="suricata_draft",
        prompt_data={
            "network_facts": [
                {"fact_id": "nf001", "field": "tls.sni", "value": "updates.example.test"}
            ]
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["rule_source"].startswith("alert tls")
    assert parsed["rule_source"].endswith(")")


def test_fallback_source_recovery_is_balanced_and_supports_suricata_actions() -> None:
    yara = parse_advisor_wire(
        """advisor-wire.v1
TYPE: yara_draft
STATUS: succeeded
rule Balanced
{
    meta:
        description = "a brace } inside a string"
    condition:
        true
}
MOTIF: $marker | measurements=M1
"""
    )
    suricata = parse_advisor_wire(
        "advisor-wire.v1\nTYPE: suricata_draft\nSTATUS: succeeded\n"
        'drop tcp any any -> any any (msg:"measured; rule"; sid:10; rev:1;) ;\n'
    )

    assert yara.source_count == 1
    assert yara.source_language == "yara"
    assert yara.source is not None and yara.source.endswith("}")
    assert suricata.source_count == 1
    assert suricata.source_language == "suricata"
    assert suricata.source == 'drop tcp any any -> any any (msg:"measured; rule"; sid:10; rev:1;) ;'


def test_incomplete_or_multiple_fallback_sources_are_never_truncated() -> None:
    incomplete = parse_advisor_wire(
        "advisor-wire.v1\nTYPE: yara_draft\nSTATUS: succeeded\n"
        "yara\n`rule Truncated { condition: true }\nMOTIF: $a | measurements=M1\n"
    )
    multiple = parse_advisor_wire(
        "advisor-wire.v1\nTYPE: yara_draft\nSTATUS: succeeded\n"
        "rule First { condition: true }\nrule Second { condition: true }\n"
    )
    parsed = parse_typed_advisor_response(
        "advisor-wire.v1\nTYPE: yara_draft\nSTATUS: succeeded\n"
        "rule First { condition: true }\nrule Second { condition: true }\n"
        "MOTIF: $a | measurements=M1\nLIMITATION: none\n",
        consultation_type="yara_draft",
        prompt_data={"measurements": [{"measurement_id": "m001", "kind": "string"}]},
        safe_text=_safe_text,
    )

    assert incomplete.source is None
    assert incomplete.source_count == 1
    assert multiple.source is None
    assert multiple.source_count == 2
    assert parsed is not None
    assert parsed["usable"] is False
    assert "raw_source_multiple_blocks" in parsed["warnings"]


def test_json_fallback_extracts_one_object_from_markdown_prose() -> None:
    assert extract_json_object('Texte intro\n```json\n{"ok":true}\n```\nfin') == {"ok": True}
    assert extract_json_object("JSON") is None


def test_draft_text_wire_groups_assertions_by_unit() -> None:
    prompt_data = {
        "units": [
            {"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
            {
                "id": "U2",
                "block_type": "uncertainties_and_next_leads",
                "form": "analyst_note",
                "anchor": "section:2:paragraph:0003",
            },
        ],
        "assertions": [
            {"id": "fact_one", "unit_id": "U1", "refs": []},
            {"id": "fact_two", "unit_id": "U2", "refs": []},
        ],
    }
    raw = """advisor-wire.v1
TYPE: draft_text
STATUS: succeeded
UNIT: U1
S: La mesure confirme le mutex observé. [A1]
UNIT: U2
S: La réserve reste circonscrite à cette ancre. [A2]
LIMITATION: none"""
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    assert parsed is not None
    assert [item["unit_id"] for item in parsed["blocks"]] == ["U1", "U2"]
    assert parsed["blocks"][0]["sentences"][0]["assertion_ids"] == ["fact_one"]
    assert parsed["blocks"][1]["sentences"][0]["assertion_ids"] == ["fact_two"]
    cross_unit = parse_typed_advisor_response(
        """advisor-wire.v1
TYPE: draft_text
STATUS: succeeded
UNIT: U1
S: La mesure confirme le mutex observé. [A2]
UNIT: U2
S: La réserve reste circonscrite à cette ancre. [A2]
LIMITATION: none""",
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    assert cross_unit is not None
    assert cross_unit["status"] == "partial"
    assert [item["unit_id"] for item in cross_unit["blocks"]] == ["U2"]


def test_draft_text_keeps_valid_units_and_treats_reference_aliases_as_prose() -> None:
    observation_ids = [str(uuid4()), str(uuid4())]
    prompt_data = {
        "units": [
            {"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
            {
                "id": "U2",
                "block_type": "additional_samples",
                "form": "analyst_note",
                "anchor": "section:2:paragraph:0003",
            },
        ],
        "assertions": [
            {
                "id": "assertion_one",
                "unit_id": "U1",
                "block_type": "key_findings",
                "form": "body",
                "anchor": None,
                "refs": [{"kind": "observation", "id": observation_ids[0], "sha256": "a" * 64}],
            },
            {
                "id": "assertion_two",
                "unit_id": "U2",
                "block_type": "additional_samples",
                "form": "analyst_note",
                "anchor": "section:2:paragraph:0003",
                "refs": [{"kind": "observation", "id": observation_ids[1], "sha256": "b" * 64}],
            },
        ],
        "transmitted_refs": [
            {"kind": "observation", "id": item, "sha256": digest}
            for item, digest in zip(observation_ids, ("a" * 64, "b" * 64), strict=True)
        ],
    }
    raw = """advisor-wire.v1
TYPE: draft_text
STATUS: partial
UNIT: U1
S: La mesure confirme un comportement observé. [A1]
UNIT: U2
S: Les observations R2 et R3 confirment la piste. [A2]
Bridge ignored prose outside the wire fields.
LIMITATION: La limite cite aussi R2 et R3 en prose.
"""

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["usable"] is True
    assert [item["unit_id"] for item in parsed["blocks"]] == ["U1", "U2"]
    assert parsed["blocks"][0]["sentences"][0]["assertion_ids"] == ["assertion_one"]
    assert parsed["blocks"][1]["sentences"][0]["text"].endswith("R2 et R3 confirment la piste.")
    assert all(item["usable"] for item in parsed["blocks"])
    assert any("R2" in item and "R3" in item for item in parsed["limitations"])
    assert "source_alias_in_prose" in parsed["warnings"]
    assert "wire_prose_ignored" in parsed["warnings"]
    assert "bridge_reported_partial" in parsed["warnings"]
    assert parsed["warning_classification"]["informative"] == [
        item
        for item in parsed["warnings"]
        if item in {"bridge_reported_partial", "source_alias_in_prose", "wire_prose_ignored"}
    ]

    json_response = {
        "schema": "advisor-response.v3",
        "consultation_type": "draft_text",
        "status": "succeeded",
        "language": "fr",
        "blocks": [
            {
                "unit_id": "U1",
                "block_type": "key_findings",
                "form": "body",
                "anchor": None,
                "sentences": [
                    {
                        "text": "La mesure confirme un comportement observé.",
                        "assertion_ids": ["assertion_one"],
                    }
                ],
            }
        ],
        "limitations": ["La limite cite R1 en prose."],
        "untrusted": True,
    }
    parsed_json = parse_typed_advisor_response(
        json.dumps(json_response, ensure_ascii=False),
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed_json is not None
    assert parsed_json["status"] == "partial"
    assert any("R1" in item for item in parsed_json["limitations"])
    assert "source_alias_in_prose" in parsed_json["warnings"]
    assert parsed_json["blocks"][0]["usable"] is True
    assert "source_alias_in_prose" in parsed_json["warning_classification"]["informative"]


def test_draft_wire_invalid_reference_tag_blocks_only_the_affected_unit() -> None:
    observation_ids = [str(uuid4()), str(uuid4())]
    prompt_data = {
        "units": [
            {"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
            {"id": "U2", "block_type": "claim_verification", "form": "body", "anchor": None},
        ],
        "assertions": [
            {
                "id": "assertion_one",
                "unit_id": "U1",
                "refs": [{"kind": "observation", "id": observation_ids[0], "sha256": "a" * 64}],
            },
            {
                "id": "assertion_two",
                "unit_id": "U2",
                "refs": [{"kind": "observation", "id": observation_ids[1], "sha256": "b" * 64}],
            },
        ],
        "transmitted_refs": [
            {"kind": "observation", "id": observation_ids[0], "sha256": "a" * 64},
            {"kind": "observation", "id": observation_ids[1], "sha256": "b" * 64},
        ],
    }
    parsed = parse_typed_advisor_response(
        """advisor-wire.v1
TYPE: draft_text
STATUS: partial
UNIT: U1
S: Une première phrase vérifiable est conservée. [A1,A99]
S: Une autre phrase vérifiable reste dans cette unité. [A1]
UNIT: U2
S: La seconde unité reste complète et vérifiable. [A2]
LIMITATION: none
""",
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed is not None
    units = {item["unit_id"]: item for item in parsed["blocks"]}
    assert units["U1"]["usable"] is False
    assert units["U2"]["usable"] is True
    assert units["U2"]["sentences"][0]["assertion_ids"] == ["assertion_two"]
    assert "assertion_alias_unknown" in parsed["warning_classification"]["blocking"]


def test_secret_detection_and_incoherent_byte_warning_block_admissibility() -> None:
    observation_id = str(uuid4())
    prompt_data = {
        "units": [{"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None}],
        "assertions": [
            {
                "id": "assertion_one",
                "unit_id": "U1",
                "refs": [{"kind": "observation", "id": observation_id, "sha256": "a" * 64}],
            }
        ],
        "transmitted_refs": [{"kind": "observation", "id": observation_id, "sha256": "a" * 64}],
    }
    secret_value = "synthetic-secret-value-for-parser-test"

    def redacting_safe_text(value: str, maximum: int) -> str:
        return value.replace(secret_value, "[REDACTED]")[:maximum].strip()

    secret_result = parse_typed_advisor_response(
        f"""advisor-wire.v1
TYPE: draft_text
STATUS: succeeded
UNIT: U1
S: Le contenu cite {secret_value} avant sa rédaction. [A1]
LIMITATION: none
""",
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=redacting_safe_text,
    )
    assert secret_result is not None
    assert secret_result["usable"] is False
    assert "secret_detected" in secret_result["warning_classification"]["blocking"]
    assert secret_value not in repr(secret_result)

    json_response = {
        "schema": "advisor-response.v3",
        "consultation_type": "draft_text",
        "status": "succeeded",
        "language": "fr",
        "warnings": ["output_byte_count_incoherent"],
        "blocks": [
            {
                "unit_id": "U1",
                "block_type": "key_findings",
                "form": "body",
                "anchor": None,
                "sentences": [
                    {
                        "text": "La mesure confirme le fait observé.",
                        "assertion_ids": ["assertion_one"],
                    }
                ],
            }
        ],
        "limitations": [],
        "untrusted": True,
    }
    byte_result = parse_typed_advisor_response(
        json.dumps(json_response, ensure_ascii=False),
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    assert byte_result is not None
    assert byte_result["usable"] is False
    assert byte_result["blocks"][0]["usable"] is False
    assert "output_byte_count_incoherent" in byte_result["warning_classification"]["blocking"]


def test_review_text_wire_returns_remarks_per_unit() -> None:
    prompt_data = {
        "units": [
            {"unit_id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
            {
                "unit_id": "U2",
                "block_type": "claim_verification",
                "form": "analyst_note",
                "anchor": "section:2:paragraph:0004",
            },
        ]
    }
    raw = """advisor-wire.v1
TYPE: review_text
STATUS: succeeded
UNIT: U1
REMARK: Séparer ce constat mesuré de son interprétation.
UNIT: U2
REMARK: Ajouter la limite de portée de cette observation.
LIMITATION: none"""
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    assert parsed is not None
    assert [item["unit_id"] for item in parsed["reviews"]] == ["U1", "U2"]
    assert parsed["reviews"][0]["remarks"] == ["Séparer ce constat mesuré de son interprétation."]
    assert all(item["usable"] for item in parsed["reviews"])


def _tolerant_review_prompt_data() -> dict[str, object]:
    return {
        "units": [
            {"unit_id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
            {
                "unit_id": "U2",
                "block_type": "additional_samples",
                "form": "body",
                "anchor": None,
            },
            {
                "unit_id": "U3",
                "block_type": "infection_chain_and_capabilities",
                "form": "body",
                "anchor": None,
            },
        ]
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Voici mon retour :\n```json\n"
            '{"units":[{"alias":"U1","verdict":"Distinguer le fait de son interprétation."}]}'
            "\n```",
            {"U1": "Distinguer le fait de son interprétation."},
        ),
        (
            '{"reviews":{"U2":{"recommendations":["Préciser la source."]},'
            '"U3":{"issues":"Ajouter la limite observée."}}}',
            {"U2": "Préciser la source.", "U3": "Ajouter la limite observée."},
        ),
        (
            '{"issues":{"U1":"Distinguer mesure et interprétation.",'
            '"U2":"Préciser la période observée."}}',
            {"U1": "Distinguer mesure et interprétation.", "U2": "Préciser la période observée."},
        ),
        (
            '{"recommendations":[{"unit":"U2","text":"Préciser la source."},'
            '{"unit":"U1","text":"Séparer le fait de son interprétation."}]}',
            {"U2": "Préciser la source.", "U1": "Séparer le fait de son interprétation."},
        ),
        (
            "{'units': [{'unit': 'U1', 'recommendations': ['Ajouter une réserve.'],},],}",
            {"U1": "Ajouter une réserve."},
        ),
        (
            "U1: Distinguer observation et hypothèse.\nUnit 2: Préciser le contexte.",
            {"U1": "Distinguer observation et hypothèse.", "U2": "Préciser le contexte."},
        ),
        (
            "- **U1:** Séparer le fait de l'interprétation.\n"
            "- Unit 2 — Ajouter la période observée.",
            {"U1": "Séparer le fait de l'interprétation.", "U2": "Ajouter la période observée."},
        ),
    ],
)
def test_review_text_recovers_bridge_json_and_labelled_prose(
    raw: str, expected: dict[str, str]
) -> None:
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["status"] == "partial"
    assert {item["unit_id"]: item["remarks"][0] for item in parsed["reviews"]} == expected
    assert "heuristic_unit_assignment" in parsed["warnings"]


def test_review_text_keeps_ambiguous_overall_as_unassigned_summary_and_raw_text() -> None:
    raw = (
        "The review is readable, but keep the measured claim separate from interpretation.\n"
        "\n## Additional context\nRetain the uncertainty around the sample scope."
    )
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["usable"] is True, parsed
    assert parsed["unit_assignment_status"] == "unstructured_overall"
    assert "unstructured_overall" in parsed["warning_classification"]["informative"]
    assert parsed["reviews"] == []
    assert parsed["overall_review"] == raw
    assert parsed["raw_output"] == raw
    assert parsed["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_review_text_assigns_exact_prose_to_a_single_explicit_target() -> None:
    raw = "Keep this measured claim separate from interpretation, and preserve its caveat."
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data={
            "units": [
                {"unit_id": "U1", "block_type": "key_findings", "form": "body", "anchor": None}
            ]
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["unit_assignment_status"] == "single_requested_unit"
    assert parsed["reviews"][0]["unit_id"] == "U1"
    assert parsed["reviews"][0]["remarks"] == [raw]
    assert parsed["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_review_json_without_unit_labels_is_not_assigned_by_array_order() -> None:
    raw = '{"reviews":["First unassigned point.","Second unassigned point."]}'
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True, parsed
    assert parsed["reviews"] == []
    assert parsed["unit_assignment_status"] == "unstructured_overall"
    assert parsed["overall_review"] == "First unassigned point.\nSecond unassigned point."


def test_review_text_preserves_complete_prose_above_legacy_64k_cap() -> None:
    raw = "Conseil lisible.\n" + ("suite complète " * 8_000)
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert len(raw.encode("utf-8")) > 64 * 1024
    assert parsed["raw_output"] == raw
    assert parsed["raw_output_bytes"] == len(raw.encode("utf-8"))
    assert parsed["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert parsed["raw_output_truncated"] is False


def test_heuristic_review_keeps_complete_unit_advice_above_legacy_remark_cap() -> None:
    advice = "Clarifier la portée du constat et conserver sa réserve probatoire. " * 100
    raw = f"U1: {advice}"
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["reviews"][0]["remarks"] == [advice.strip()]


def test_heuristic_draft_keeps_complete_unit_text_above_legacy_remark_cap() -> None:
    draft = "Les observations indiquent une séquence cohérente à confirmer. " * 100
    parsed = parse_typed_advisor_response(
        f"U1: {draft}",
        consultation_type="draft_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["blocks"][0]["sentences"][0]["text"] == draft.strip()


def test_review_text_keeps_advice_while_redacting_operational_secret_fields() -> None:
    token = "synthetic-advisor-token-for-redaction"
    raw = json.dumps(
        {
            "summary": "Clarify the scope of the observation.",
            "openai_bridge_api_key": token,
        }
    )
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
        operational_secret_values=(token,),
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert token not in parsed["raw_output"]
    assert token not in repr(parsed["reviews"])
    assert "[clé masquée]" in parsed["raw_output"]
    assert "secret_redacted" in parsed["warnings"]


@pytest.mark.parametrize("raw", ["", " \n ", "{}", "JSON", "...", "{} {}"])
def test_review_text_empty_or_garbage_remains_unusable(raw: str) -> None:
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is False


def test_draft_text_recovers_labelled_bridge_prose_by_unit() -> None:
    parsed = parse_typed_advisor_response(
        "U1: La méthode confirme l'ordre des transformations observées.\n"
        "Unit 2 — Le contexte temporel reste à préciser.",
        consultation_type="draft_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["status"] == "partial"
    assert parsed["unit_assignment_status"] == "heuristic"
    assert [block["unit_id"] for block in parsed["blocks"]] == ["U1", "U2"]
    assert [block["sentences"][0]["text"] for block in parsed["blocks"]] == [
        "La méthode confirme l'ordre des transformations observées.",
        "Le contexte temporel reste à préciser.",
    ]
    assert all(block["sentences"][0]["assertion_ids"] == [] for block in parsed["blocks"])
    assert "unstructured_draft" in parsed["warning_classification"]["informative"]


def test_draft_text_keeps_unstructured_overall_without_per_unit_blocks() -> None:
    raw = "The observations support a staged execution sequence, with timing still uncertain."
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="draft_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["unit_assignment_status"] == "unstructured_overall"
    assert parsed["summary"] == raw
    assert "blocks" not in parsed
    assert "unstructured_overall" in parsed["warning_classification"]["informative"]


def test_draft_text_single_explicit_target_keeps_exact_prose_and_sha() -> None:
    raw = "Keep this passage as a consultative draft with its original caveat."
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="draft_text",
        prompt_data={
            "units": [
                {"unit_id": "U1", "block_type": "key_findings", "form": "body", "anchor": None}
            ]
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["unit_assignment_status"] == "single_requested_unit"
    assert parsed["blocks"][0]["sentences"][0]["text"] == raw
    assert parsed["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("raw", ["", "   ", "{}", "...", "null"])
def test_draft_text_empty_or_garbage_remains_unusable(raw: str) -> None:
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="draft_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is None or parsed["usable"] is False


def test_review_wire_keeps_complete_unit_usable_in_partial_lot() -> None:
    prompt_data = {
        "units": [
            {"unit_id": "U13", "block_type": "key_findings", "form": "body", "anchor": None},
            {"unit_id": "U15", "block_type": "claim_verification", "form": "body", "anchor": None},
        ]
    }
    parsed = parse_typed_advisor_response(
        """advisor-wire.v1
TYPE: review_text
STATUS: partial
UNIT: U13
REMARK: La remarque cite R2 en prose, sans ajouter de référence.
""",
        consultation_type="review_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["usable"] is True
    assert [item["unit_id"] for item in parsed["reviews"]] == ["U13"]
    assert parsed["reviews"][0]["usable"] is True
    assert "R2" in parsed["reviews"][0]["remarks"][0]
    assert "source_alias_in_prose" in parsed["warning_classification"]["informative"]
    assert "limitations_missing" not in parsed["warnings"]
    assert "review_unit_missing" in parsed["warning_classification"]["blocking"]


def test_partial_reference_list_does_not_downgrade_usable_review_advice() -> None:
    parsed = parse_typed_advisor_response(
        """advisor-wire.v1
TYPE: review_text
STATUS: succeeded
UNIT: U1
REMARK: Clarifier la portée de l'observation.
REFS: R99
""",
        consultation_type="review_text",
        prompt_data={
            "units": [{"unit_id": "U1", "block_type": "key_findings"}],
            "dossier_manifest": [],
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "succeeded"
    assert parsed["normalization_status"] == "complete"
    assert parsed["usable"] is True
    assert parsed["reviews"][0]["usable"] is True
    assert "reference_alias_unknown" in parsed["warning_classification"]["informative"]
    assert "reference_list_partial" in parsed["warning_classification"]["informative"]
    assert "reference_alias_unknown" not in parsed["warning_classification"]["blocking"]
    assert "reference_list_partial" not in parsed["warning_classification"]["blocking"]


def test_review_wire_omits_empty_limitation_without_warning() -> None:
    parsed = parse_typed_advisor_response(
        """advisor-wire.v1
TYPE: review_text
STATUS: succeeded
UNIT: U13
REMARK: La méthode confirme l\u2019ordre des transformations observées.
REFS: E1
""",
        consultation_type="review_text",
        prompt_data={
            "units": [{"unit_id": "U13", "block_type": "key_findings"}],
            "dossier_manifest": [
                {
                    "alias": "E1",
                    "kind": "observation",
                    "id": "observation-1",
                    "sha256": "a" * 64,
                }
            ],
        },
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "succeeded"
    assert parsed["limitations"] == []
    assert "limitations_missing" not in parsed["warnings"]


def test_plain_draft_maps_to_the_only_explicit_target_without_changing_text() -> None:
    raw = (
        "### 1. Échantillon A\n"
        "Les octets statiques indiquent un accès possible à GitHub Contents. [E1]\n"
        "Cela ne prouve aucune connexion réelle."
    )
    target = {
        "id": "U1",
        "block_type": "key_findings",
        "form": "body",
        "anchor": None,
    }
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="draft_text",
        prompt_data={"units": [target]},
        safe_text=_safe_text,
    )
    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed["unit_assignment_status"] == "single_requested_unit"
    assert parsed["blocks"][0]["block_type"] == "key_findings"
    assert parsed["blocks"][0]["sentences"] == [{"text": raw, "assertion_ids": []}]


def test_plain_draft_multiple_targets_are_not_guessed_by_section_order() -> None:
    raw = (
        "### 1. Échantillon A\nObservation une. [E1]\n### 2. Échantillon B\nObservation deux. [E2]"
    )
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="draft_text",
        prompt_data={
            "units": [
                {"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
                {
                    "id": "U2",
                    "block_type": "proposed_detections",
                    "form": "body",
                    "anchor": None,
                },
            ]
        },
        safe_text=_safe_text,
    )
    assert parsed is not None
    assert parsed["usable"] is True
    assert parsed.get("blocks", []) == []
    assert parsed["unit_assignment_status"] == "unstructured_overall"


def test_draft_prompt_requests_literal_unit_headings_in_prose() -> None:
    prompt = render_advisor_prompt(
        "draft_text",
        {
            "question": "Rédiger des faits techniques.",
            "units": [{"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None}],
        },
    )
    assert "Ne pas remplacer U1/U2" in prompt
    assert "U1 — Faits clés" in prompt
    assert prompt.index("Ne pas remplacer U1/U2") < prompt.index("FAITS TRANSMIS")


def test_yara_dossier_keeps_measured_bytes_and_article_context() -> None:
    evidence_id = str(uuid4())
    dossier = build_advisor_dossier(
        {
            "consultation_type": "yara_draft",
            "question": "Détecter api.github.com et le motif 4d5a observé.",
            "article_sha256": "a" * 64,
            "article_passages": [
                {
                    "claim_id": "S1P1",
                    "heading": "Canal GitHub",
                    "text": "L'article décrit un canal api.github.com à vérifier.",
                }
            ],
            "evidence": [
                {
                    "evidence_id": evidence_id,
                    "sha256": "b" * 64,
                    "source": "sample.analysis",
                    "object_type": "file",
                    "content_excerpts": [
                        {"title": "Chaînes décodées", "text": "api.github.com"},
                    ],
                }
            ],
            "measurements": [
                {
                    "measurement_id": "m001",
                    "evidence_id": evidence_id,
                    "evidence_sha256": "b" * 64,
                    "sample_sha256": "c" * 64,
                    "kind": "hex",
                    "offset": 0,
                    "value": "4d5a",
                }
            ],
        }
    )
    assert "api.github.com" in dossier.text
    assert "4d5a" in dossier.text
    assert "offset 0" in dossier.text
    assert "Canal GitHub" in dossier.text
    assert any(row["kind"] == "measurement" for row in dossier.manifest)


def test_draft_prose_maps_explicit_named_sections_not_guessing_order() -> None:
    parsed = parse_typed_advisor_response(
        "### 2. Détections proposées\nYARA expérimentale. [E2]\n"
        "### 1. Faits clés\nLe code est statique. [E1]",
        consultation_type="draft_text",
        prompt_data={
            "units": [
                {"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
                {"id": "U2", "block_type": "proposed_detections", "form": "body", "anchor": None},
            ]
        },
        safe_text=_safe_text,
    )
    assert parsed is not None
    assert parsed["usable"] is True
    assigned = {block["block_type"]: block for block in parsed["blocks"]}
    assert "YARA expérimentale" in assigned["proposed_detections"]["sentences"][0]["text"]
    assert "Le code est statique" in assigned["key_findings"]["sentences"][0]["text"]
