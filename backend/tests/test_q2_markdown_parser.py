"""Tolerance tests for `parse_q2_proposals_markdown` (P23.7).

The ChatGPT bridge does not guarantee response_format / JSON Schema: it
drives an ordinary conversation and hands back rendered prose. Q2 therefore
asks for the same tolerant Markdown dialect Q1 already uses. Unlike Q1
though, an unreadable proposal here is a *structural loss*: it lands in
`errors` (failing `ParseResult.usable`), which is what drives the one-shot
repair turn in `_ask_with_format_repair` — the parser is permissive about
syntax, never about missing structure. It never checks a proposal against the
archived source; that proof stays inside `verify_q2_proposals`.
"""

from __future__ import annotations

from cti_app.application.production_parsers import parse_q2_proposals_markdown

CANONICAL = """# FACT
category: malware
value: ExampleRAT
context: outil de charge utile
evidence-quote: ExampleRAT a ete deploye sur les postes compromis
attack-id: T1059.001

# ARTIFACT
artifact-type: domain
value: evil.example.com
indicator-status: confirmed_ioc
context: domaine C2
evidence-quote: le domaine C2 evil.example.com a ete identifie comme IOC

# UNCERTAINTIES
- attribution incertaine
"""


def test_canonical_markdown_is_parsed() -> None:
    result = parse_q2_proposals_markdown(CANONICAL)
    assert result.usable, result.errors
    assert result.value is not None
    assert len(result.value.facts) == 1
    assert len(result.value.artifacts) == 1
    fact = result.value.facts[0]
    assert fact.category == "malware"
    assert fact.attack_id == "T1059.001"
    artifact = result.value.artifacts[0]
    assert artifact.artifact_type == "domain"
    assert artifact.indicator_status == "confirmed_ioc"
    assert result.value.uncertainties == ["attribution incertaine"]
    assert not result.warnings
    assert not result.errors


def test_markdown_escapes_are_tolerated_without_touching_windows_paths() -> None:
    text = """# FACT
category: infection\\_chain
value: C:\\Windows\\System32
context: chemin observe
evidence: chemin observe dans la source

# ARTIFACT
artifact-type: filepath
value: C:\\inetpub\\wwwroot
indicator-status: contextual
context: repertoire observe
evidence: tableau technique
location: table
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.facts[0].category == "infection_chain"
    assert result.value.artifacts[0].value == r"C:\inetpub\wwwroot"


def test_headings_without_hash_markers_are_recognized() -> None:
    """The bridge serialises the rendered DOM: headings routinely lose `#`."""
    text = """FACT
category: tools
value: Mimikatz
context: outil de dump d'identifiants
evidence-quote: Mimikatz a servi a dumper les identifiants

ARTIFACT
artifact-type: ip
value: 203.0.113.7
indicator-status: contextual
context: IP mentionnee
evidence-quote: l'adresse 203.0.113.7 est mentionnee dans le rapport
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert len(result.value.facts) == 1
    assert len(result.value.artifacts) == 1


def test_french_and_english_field_names_mix_freely() -> None:
    text = """# FACT
category: campaigns
valeur: Operation Exemple
contexte: campagne observee
evidence: Operation Exemple a cible plusieurs organisations

# ARTIFACT
type: email
value: attacker@evil.example
status: confirmed_ioc
contexte: adresse d'origine
quote: l'email attacker@evil.example a servi a l'hameconnage IOC confirme
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.facts[0].value == "Operation Exemple"
    assert result.value.artifacts[0].value == "attacker@evil.example"


def test_field_order_does_not_matter() -> None:
    text = """# ARTIFACT
evidence-quote: le hash abc123 correspond au fichier malveillant abc123
context: hash de charge utile
indicator-status: confirmed_ioc
value: abc123
artifact-type: sha1
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.artifacts[0].artifact_type == "hash"


def test_multiline_field_values_are_joined() -> None:
    text = """# FACT
category: ttps
value: Persistance via tache planifiee
context: mecanisme de persistance
  observe sur plusieurs hotes
  du reseau compromis
evidence-quote: une tache planifiee malveillante a ete deployee
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert "plusieurs hotes" in result.value.facts[0].context
    assert "reseau compromis" in result.value.facts[0].context


def test_outer_code_fence_is_stripped() -> None:
    text = "```\n" + CANONICAL + "\n```"
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert len(result.value.facts) == 1


def test_extra_prose_around_blocks_is_ignored() -> None:
    text = f"""Voici mon analyse du corpus fourni.

{CANONICAL}

J'espere que cette extraction repond a vos attentes.
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert len(result.value.facts) == 1
    assert len(result.value.artifacts) == 1


def test_unknown_fields_are_ignored_with_a_warning() -> None:
    text = """# FACT
category: malware
value: ExampleRAT
context: outil
evidence-quote: ExampleRAT a ete observe
confidence: high
source-reliability: A1

# ARTIFACT
artifact-type: domain
value: evil.example.com
indicator-status: contextual
context: domaine mentionne
evidence-quote: evil.example.com apparait dans le rapport
mitre-tactic: command-and-control
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert any("unknown_field_ignored:confidence" in w for w in result.warnings)
    assert any("unknown_field_ignored:source-reliability" in w for w in result.warnings)
    assert any("unknown_field_ignored:mitre-tactic" in w for w in result.warnings)


def test_evidence_field_aliases_are_all_accepted() -> None:
    for alias in ("evidence-quote", "evidence", "quote", "citation"):
        text = f"""# ARTIFACT
artifact-type: cve
value: CVE-2024-1234
indicator-status: contextual
context: vulnerabilite exploitee
{alias}: CVE-2024-1234 a ete exploitee dans la campagne
"""
        result = parse_q2_proposals_markdown(text)
        assert result.usable, (alias, result.errors)
        assert result.value is not None
        assert result.value.artifacts[0].evidence_quote


def test_artifact_type_aliases_resolve_to_the_canonical_hash_type() -> None:
    for alias in ("md5", "sha1", "sha256", "sha512", "hash"):
        text = f"""# ARTIFACT
artifact-type: {alias}
value: deadbeef
indicator-status: contextual
context: valeur de hachage
evidence-quote: le hash deadbeef a ete extrait
"""
        result = parse_q2_proposals_markdown(text)
        assert result.usable, (alias, result.errors)
        assert result.value is not None
        assert result.value.artifacts[0].artifact_type == "hash"


def test_multiple_facts_and_artifacts_are_all_kept() -> None:
    text = """# FACT
category: actors
value: Acteur Alpha
context: acteur identifie
evidence-quote: Acteur Alpha a revendique l'operation

# FACT
category: tools
value: Cobalt Strike
context: framework offensif
evidence-quote: un beacon Cobalt Strike a ete deploye

# ARTIFACT
artifact-type: domain
value: c2.example.net
indicator-status: confirmed_ioc
context: domaine C2
evidence-quote: le C2 c2.example.net est un IOC confirme

# ARTIFACT
artifact-type: ip
value: 198.51.100.20
indicator-status: excluded
context: IP de test
evidence-quote: l'IP 198.51.100.20 est un exemple utilise pour les tests
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert len(result.value.facts) == 2
    assert len(result.value.artifacts) == 2


def test_one_malformed_block_forces_repair_without_losing_valid_ones() -> None:
    """Unlike Q1, a bad block here does not degrade silently: it flips
    `usable` to False so the caller repairs, but the good proposals are still
    present for diagnostics/counts on that first parse."""
    text = """# FACT
category: malware
value: ExampleRAT
context: outil
evidence-quote: ExampleRAT a ete deploye

# ARTIFACT
artifact-type: domain
value: evil.example.com
indicator-status: excluded
context: faux positif

# ARTIFACT
artifact-type: cve
value: CVE-2024-9999
indicator-status: contextual
context: vulnerabilite
evidence-quote: CVE-2024-9999 a ete mentionnee dans le rapport
"""
    result = parse_q2_proposals_markdown(text)
    assert not result.usable
    assert "artifact_without_evidence_quote" in result.errors
    assert result.value is not None
    assert len(result.value.facts) == 1
    assert len(result.value.artifacts) == 1  # the CVE block, not the broken domain block


def test_missing_value_is_a_structural_loss() -> None:
    text = """# FACT
category: malware
context: outil sans valeur
evidence-quote: un outil a ete observe
"""
    result = parse_q2_proposals_markdown(text)
    assert not result.usable
    assert "fact_without_value" in result.errors


def test_missing_evidence_quote_is_a_structural_loss() -> None:
    text = """# ARTIFACT
artifact-type: domain
value: evil.example.com
indicator-status: contextual
context: domaine sans preuve
"""
    result = parse_q2_proposals_markdown(text)
    assert not result.usable
    assert "artifact_without_evidence_quote" in result.errors


def test_unknown_artifact_type_is_a_structural_loss() -> None:
    text = """# ARTIFACT
artifact-type: registry_key
value: HKLM\\Software\\Evil
indicator-status: contextual
context: cle de registre
evidence-quote: la cle HKLM\\Software\\Evil a ete creee
"""
    result = parse_q2_proposals_markdown(text)
    assert not result.usable
    assert "unknown_artifact_type" in result.errors


def test_unrecognized_indicator_status_defaults_conservatively() -> None:
    """A misspelled/missing status is a recoverable default, not a structural
    loss: it must never silently become confirmed_ioc."""
    text = """# ARTIFACT
artifact-type: domain
value: evil.example.com
indicator-status: maybe_ioc
context: statut douteux
evidence-quote: evil.example.com est peut-etre un IOC
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.artifacts[0].indicator_status == "contextual"
    assert "indicator_status_defaulted" in result.warnings


def test_uncertainties_section_is_collected() -> None:
    text = CANONICAL
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.uncertainties == ["attribution incertaine"]


def test_uncertainties_section_is_optional() -> None:
    text = """# FACT
category: malware
value: ExampleRAT
context: outil
evidence-quote: ExampleRAT a ete deploye
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.uncertainties == []


def test_fake_json_wrapping_valid_markdown_still_parses_the_markdown() -> None:
    text = '{"answer": "' + CANONICAL.replace("\n", "\\n") + '"}'
    result = parse_q2_proposals_markdown(text)
    # The whole thing reads as prose with no recognizable FACT/ARTIFACT
    # heading (the markers are inside an escaped JSON string) -> unusable,
    # never silently mis-parsed as if it were real JSON.
    assert not result.usable
    assert "no_fact_or_artifact_block" in result.errors


def test_json_object_stray_inside_a_block_does_not_sink_other_blocks() -> None:
    """Mirrors the real Qwen production failure this migration responds to:
    a `{"artifacts": [...]}` group landed inside a facts array and made the
    whole strict-JSON answer unparseable. In the permissive Markdown dialect,
    a stray JSON fragment inside one block must not take down the rest of the
    answer -- either it is absorbed harmlessly as prose, or that one block is
    dropped, never both other valid blocks."""
    text = """# ARTIFACT
artifact-type: domain
value: evil.example.com
indicator-status: contextual
context: domaine mentionne
evidence-quote: le domaine evil.example.com est mentionne
{"artifacts": [{"value": "stray.example", "artifact_type": "domain"}]}

# ARTIFACT
artifact-type: ip
value: 198.51.100.30
indicator-status: excluded
context: exemple
evidence-quote: l'IP 198.51.100.30 est un exemple
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert len(result.value.artifacts) == 2
    values = {a.value for a in result.value.artifacts}
    assert "evil.example.com" in values
    assert "198.51.100.30" in values
    assert "stray.example" not in values


def test_empty_answer_is_unusable() -> None:
    result = parse_q2_proposals_markdown("")
    assert not result.usable
    assert "empty_response" in result.errors


def test_answer_with_no_fact_or_artifact_block_is_unusable() -> None:
    result = parse_q2_proposals_markdown("Je n'ai rien trouve d'exploitable dans ce corpus.")
    assert not result.usable
    assert "no_fact_or_artifact_block" in result.errors


def test_parser_never_checks_the_evidence_against_a_source() -> None:
    """Structural validity only: whether the quote is actually in the
    archived text is entirely `verify_q2_proposals`' job."""
    text = """# ARTIFACT
artifact-type: domain
value: totally-fabricated-value.example
indicator-status: confirmed_ioc
context: valeur inventee
evidence-quote: une citation qui ne contient meme pas la valeur
"""
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.artifacts[0].value == "totally-fabricated-value.example"
