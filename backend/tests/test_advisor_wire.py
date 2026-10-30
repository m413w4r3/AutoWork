from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

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
            "sha256": "a" * 64,
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
    passages = _consultation_article_passages(snapshot, "Compare S3P2.1 and S4P1.1 with S9P9.")
    dossier = build_advisor_dossier(
        {
            "consultation_type": "figure_plan",
            "question": "Compare S3P2.1 and S4P1.1 with S9P9.",
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
    assert "S9P9" not in {row["claim_id"] for row in article_rows}


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

    prompt_data = {"transmitted_refs": [{"kind": "article", "id": article_sha, "sha256": article_sha}]}
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
    assert parsed["figure_plan"]["nodes"][0]["refs"] == [
        {"kind": "article", "id": article_sha}
    ]
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


def test_review_text_attaches_unstructured_overall_to_each_requested_unit() -> None:
    raw = "The review is readable, but keep the measured claim separate from interpretation."
    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="review_text",
        prompt_data=_tolerant_review_prompt_data(),
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["status"] == "partial"
    assert parsed["usable"] is True
    assert parsed["unit_assignment_status"] == "unstructured_overall"
    assert "unstructured_overall" in parsed["warning_classification"]["informative"]
    assert {item["unit_id"] for item in parsed["reviews"]} == {"U1", "U2", "U3"}
    assert all(item["remarks"] == [raw] for item in parsed["reviews"])
    assert all(item["assignment"] == "unstructured_overall" for item in parsed["reviews"])
    assert parsed["raw_output"] == raw


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
