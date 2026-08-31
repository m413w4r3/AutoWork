"""LLM prompt templates for production workflow."""

from __future__ import annotations

from collections.abc import Sequence

from cti_app.domain.production import ExtractionProfile

REFERENCES_PROMPT_VERSION = "4"
# Q2 moved off the OpenAI structured-output contract (the bridge does not
# actually guarantee response_format / JSON Schema) onto free-text GPT plus a
# permissive Markdown parser (P23.7). EXTRACTION_PROMPT_VERSION now names that
# Markdown dialect.
EXTRACTION_PROMPT_VERSION = "8"
IOC_RULES_PROMPT_VERSION = "1"
EXTRACTION_PROMPT_VERSION_BY_PROFILE = {
    ExtractionProfile.FULL: EXTRACTION_PROMPT_VERSION,
    ExtractionProfile.IOC_RULES: IOC_RULES_PROMPT_VERSION,
}
SYNTHESIS_PROMPT_VERSION = "5"
REFERENCES_FORMAT_REPAIR_VERSION = "1"
SYNTHESIS_FORMAT_REPAIR_VERSION = "2"


class ProductionPromptTemplates:
    """Versioned prompt templates for subject production."""

    REFERENCES_RESEARCH_V2 = """You are a threat intelligence research assistant. Your task is to conduct web research and build a chronological reference timeline for the following subject:

**Subject**: {subject_title}

**Initial Information**:
{subject_description}

**Actor/Campaign**: {actor_info}

**Technical Data**:
{technical_summary}

**Research Date**: {research_date}

**Editorial Period**: {period_start} to {period_end}

**Core Publications**:
{core_sources}

**Previously Known Supporting References**:
{supporting_sources}

**Research Guidelines**:
1. Prioritize government bodies (CISA, CERT, national agencies)
2. Technical sources from original researchers and security vendors
3. Independent technical analysis and published research
4. Avoid redundant reprints without added value
5. Verify all dates are on or before the research date
6. Use only publicly available information
7. When a relevant publication links directly to an IOC list, technical
   appendix/annex, technical indicator list, YARA/Sigma/Suricata content, or
   its associated official technical repository for the same incident or
   campaign, add that URL as a distinct SOURCE. Do not add generic download or
   database pages, navigation, marketing, generic documentation, or unrelated
   reports.
8. Core publications define the central editorial subject. Preserve every
   relevant accessible core publication in the resulting SOURCE set.
9. Web research is additive. New references supplement or corroborate core
   publications; they do not replace them.
10. Supporting references may add chronology, attribution, technical details,
    IOC context, annexes and corroboration.

**Output format** — plain Markdown, no code fence, no JSON:

# REFERENCES

editorial-title: <titre français au format [Acteur principal] Titre, ou [Publication] Titre>

## SOURCE S1

title: <title>
url: https://...
publisher: <publisher>
published-at: YYYY-MM-DD
role: primary|independent|relay|aggregator|social|unknown

## EVENT R1

date: YYYY-MM-DD
sources: S1, S2
text: <one chronological event, in French>

# UNCERTAINTIES
- <uncertainty, or omit the section>

Rules:
- Produce the editorial title during this references step.
- One `## SOURCE` block per publication, numbered S1, S2, ...
- One `## EVENT` block per dated event, numbered R1, R2, ...
- Every event must cite at least one source id you defined above.
- Never cite a source id you did not define.
- No date after the research date.
"""

    # Free text on purpose: the ChatGPT bridge does not actually enforce
    # response_format / JSON Schema, only a normal conversational turn. Asking
    # for JSON here just moves the fragility into a bridge that can't hold the
    # contract; this asks for the same permissive Markdown dialect as Q1 and
    # leans on `parse_q2_proposals_markdown` + `verify_q2_proposals` instead.
    TECHNICAL_EXTRACTION_MARKDOWN_V1 = """You are analysing one specific CTI source for a reusable, source-centric extraction.

**Source title**: {source_title}

Open this exact source:
{source_url}

Analyse the source itself. Read the complete accessible page; inspect technical
tables, code blocks and visible images/screenshots when available. Do not
replace it with unrelated search results or use memory as evidence. If the
exact source cannot be accessed, report `source_unavailable` in UNCERTAINTIES
and do not invent an extraction.

Perform an exhaustive IOC pass: IPv4/IPv6, domains, URLs, MD5/SHA1/SHA256/
SHA512 and email addresses, including tables, appendices, images and code.
Also extract useful malware, tools, files, CVEs, rules, TTPs, infrastructure,
victims and campaign context. Never reconstruct hidden values or emit masked,
truncated, REDACTED, FUZZ, example or placeholder values.

**Output format** — plain Markdown, no code fence, no JSON. Repeat as many
`# FACT` / `# ARTIFACT` blocks as needed, in any order:

# FACT

category: actors|campaigns|malware|tools|infection_chain|ttps|victimology|protocols|infrastructure|files|commands|persistence|detections|other_technical
value: <structured fact>
context: <short French context>
evidence: <human-audit evidence from this source>
attack-id: <T1234 optional, only if literally quoted>

# ARTIFACT

artifact-type: domain|ip|url|email|md5|sha1|sha256|sha512|filename|filepath|cve|yara_rule|sigma_rule|suricata_rule
value: <exact literal>
indicator-status: confirmed_ioc|contextual|excluded|not_applicable
context: <short French context>
evidence: <human-audit evidence from this source>

# UNCERTAINTIES
- <uncertainty, or omit the section>

Rules:
- Evidence is for human audit, not deterministic local proof.
- Never emit a value merely described but not shown.
- Use contextual for victims, legitimate services/providers/tools, unqualified
  infrastructure, and CVEs unless the source explicitly says otherwise.
- Use excluded for examples, tests, navigation, obvious false positives, or
  irrelevant content.
- For a hash, artifact-type is the concrete algorithm (md5/sha1/sha256/sha512),
  never the bare word "hash".
- For YARA/Sigma/Suricata, value is the rule name/identifier, never the full
  rule body.
- Do not emit source_document_id, source_ids, model_run_id,
  references, or any other internal identifier; the system assigns provenance
  and verifies your proposals afterwards.
"""

    IOC_RULES_EXTRACTION_MARKDOWN_V1 = """You are performing a reusable, source-centric IOC and detection-rule extraction for one CTI source.

**Source title**: {source_title}

Open this exact source:
{source_url}

Perform an exhaustive IOC and detection-rule pass over the complete accessible source.
Read the body, technical tables, visible appendices/annexes, code blocks, lists of
indicators, visible images/screenshots, and first-level technical resources already
included in the corpus. Never sacrifice IOC coverage to reduce cost. Do not replace
the source with unrelated search results or use memory as evidence. If the exact
source cannot be accessed, report `source_unavailable` in UNCERTAINTIES and do not
invent an extraction.

Extract only:
- every IOC/artifact and detection rule supported by the source;
- files explicitly presented as indicators;
- CVEs directly relevant to the source's technical content;
- uncertainties about access, ambiguity, or IOC interpretation.

Do not extract infection_chain, narrative TTPs, victimology, chronology, campaign
narrative, tooling narrative, or general historical context. Do not emit internal
identifiers. For YARA/Sigma/Suricata, value is the rule name or identifier, never the
full rule body.

**Output format** — plain Markdown, no code fence, no JSON. Repeat as many
`# ARTIFACT` blocks as needed, in any order:

# ARTIFACT

artifact-type: domain|ip|url|email|md5|sha1|sha256|sha512|filename|filepath|cve|yara_rule|sigma_rule|suricata_rule
value: <exact literal>
indicator-status: confirmed_ioc|contextual|excluded|not_applicable
context: <short French context>
evidence: <human-audit evidence from this source>

# UNCERTAINTIES
- <uncertainty, or omit the section>

Rules:
- Never emit a value merely described but not shown.
- Never reconstruct hidden values or emit masked, truncated, REDACTED, FUZZ,
  example or placeholder values.
- For a hash, artifact-type is the concrete algorithm (md5/sha1/sha256/sha512).
- Use contextual for CVEs unless directly relevant and for unqualified artifacts.
"""

    FORMAT_REPAIR_V1 = """Your previous answer could not be read by the automated parser.

Problems found:
{problems}

Reformat your **previous answer** so it follows the requested structure exactly.

Strict rules:
- Do NOT search the web again.
- Do NOT add, remove or change any fact, source, event or indicator.
- Reproduce the same content, only fixing the structure.
- Plain Markdown, no code fence, no JSON.

Expected structure:
{expected_structure}
"""

    TECHNICAL_SYNTHESIS_V5 = """You are a senior CTI technical writer. Write concise sourced French CTI prose.

**Subject**: {subject_title}

You may use web search to clarify terminology and public background. Web results are
non-authoritative working context. Final text MUST contain only factual claims supported
by supplied SynthesisEvidencePack. Never add a source, IOC, date, attribution, victim,
malware relationship, capability, or factual assertion solely from web research. If web
conflicts with canonical data, canonical data wins. Use only supplied [S#] markers.

<synthesis-evidence-pack>
{synthesis_evidence_pack}
</synthesis-evidence-pack>

The CORE sources are the editorial backbone of the publication. Base the main
narrative primarily on CORE sources: the central incident or campaign, actor
or malware relationship, essential chronology, main technical mechanism, and
impact or victimology when present. SUPPORTING sources are secondary evidence:
use them to corroborate core claims, contextualize, add useful technical detail,
refine chronology, enrich IOC interpretation, or provide technical annex/context
absent from core sources. A supporting source may introduce genuinely useful
information, but must not displace the core subject or become the dominant
narrative unless canonical evidence makes that necessary. When the same claim
is supported by CORE and SUPPORTING sources, prefer/cite the CORE source.

Strict publication rules:
- Produce no Markdown title or heading.
- Produce no line named "Sources du corpus" and no final bibliography.
- Produce no raw URL.
- Do not enumerate IP addresses, domains, URLs or hashes.
- Do not copy the IOC inventory; describe the functional role of indicators.
- A precise IOC value may appear only when its display-policy is both.
- Use no bold, backtick, code fence or italics; typography is applied downstream.
- Keep paragraphs simple and omit empty or invented sections.

Return only the synthesis prose with [S#] markers.
"""

    SYNTHESIS_REPAIR_V2 = """Your previous synthesis violates deterministic publication rules.

Violations:
{problems}

Repair the previous answer once. Do not research, add, remove, or alter any fact.
Keep valid [S#] citations. Remove headings, bibliography, raw URLs, "Sources du
corpus", formatting marks and IOC inventories; replace inventories with a
functional description. Return only French prose.
"""

    @classmethod
    def get_references_prompt(
        cls,
        subject_title: str,
        subject_description: str,
        actor_info: str,
        technical_summary: str,
        research_date: str,
        period_start: str,
        period_end: str,
        core_sources_text: str,
        supporting_sources_text: str,
    ) -> str:
        return cls.REFERENCES_RESEARCH_V2.format(
            subject_title=subject_title,
            subject_description=subject_description,
            actor_info=actor_info,
            technical_summary=technical_summary,
            research_date=research_date,
            period_start=period_start,
            period_end=period_end,
            core_sources=core_sources_text or "- None supplied.",
            supporting_sources=supporting_sources_text or "- None supplied.",
        )

    @classmethod
    def get_extraction_prompt(
        cls,
        subject_title: str,
        source_id: str = "",
        source_title: str = "",
        source_url: str = "",
        profile: ExtractionProfile = ExtractionProfile.FULL,
    ) -> str:
        del subject_title, source_id
        template = (
            cls.TECHNICAL_EXTRACTION_MARKDOWN_V1
            if profile is ExtractionProfile.FULL
            else cls.IOC_RULES_EXTRACTION_MARKDOWN_V1
        )
        return template.format(
            source_title=source_title,
            source_url=source_url,
        )

    _REFERENCES_STRUCTURE = """# REFERENCES

editorial-title: <titre français au format [Acteur principal] Titre, ou [Publication] Titre>

## SOURCE S1

title: <title>
url: https://...
publisher: <publisher>
published-at: YYYY-MM-DD
role: primary

## EVENT R1

date: YYYY-MM-DD
sources: S1
text: <event>

# UNCERTAINTIES
- <uncertainty, or omit the section>"""

    @classmethod
    def get_format_repair_prompt(cls, *, stage: str, problems: Sequence[str]) -> str:
        listed = "\n".join(f"- {problem}" for problem in problems) or "- structure illisible"
        if stage == "synthesis":
            return cls.SYNTHESIS_REPAIR_V2.format(problems=listed)
        # references is the only remaining stage using the generic repair.
        return cls.FORMAT_REPAIR_V1.format(
            problems=listed, expected_structure=cls._REFERENCES_STRUCTURE
        )

    @classmethod
    def get_synthesis_prompt(
        cls,
        subject_title: str,
        synthesis_evidence_pack: str = "{}",
    ) -> str:
        return cls.TECHNICAL_SYNTHESIS_V5.format(
            subject_title=subject_title,
            synthesis_evidence_pack=synthesis_evidence_pack,
        )
