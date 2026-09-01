"""LLM prompt templates for production workflow."""

from __future__ import annotations

from collections.abc import Sequence

from cti_app.domain.production import ExtractionProfile

REFERENCES_PROMPT_VERSION = "5"
# Q2 uses free-text GPT plus a stateless Markdown wire-format parser. The bridge does
# not guarantee response_format / JSON Schema.
# "15" / "8": Q2 analyses the live publication behind the exact canonical URL,
# including its rendered tables, code and visible images. The local archive is
# collection provenance and is never inlined in the prompt.
EXTRACTION_PROMPT_VERSION = "15"
IOC_RULES_PROMPT_VERSION = "8"
# "7": the batch input is the compact list of exact source URLs. Only the
# output stays marker-framed: a marker starts the next block; EOF closes the
# final block.
IOC_RULES_BATCH_PROMPT_VERSION = "7"
EXTRACTION_PROMPT_VERSION_BY_PROFILE = {
    ExtractionProfile.FULL: EXTRACTION_PROMPT_VERSION,
    ExtractionProfile.IOC_RULES: IOC_RULES_PROMPT_VERSION,
}
SYNTHESIS_PROMPT_VERSION = "5"
REFERENCES_FORMAT_REPAIR_VERSION = "1"
SYNTHESIS_FORMAT_REPAIR_VERSION = "2"


_Q2_WIRE_FORMAT = """FACT <category>
- <value>
- <value> :: <short context>

IOC <confirmed|contextual> <type>
- <value>

RULE <yara|sigma|suricata|snort>[: <visible name>]
```<language>
<literal body>
```

UNCERTAINTIES
- <uncertainty>

Or, when applicable, return exactly one of these terminal responses:
EMPTY

UNAVAILABLE"""

_Q2_IOC_RULES_WIRE_FORMAT = """IOC <confirmed|contextual> <type>
- <value>

RULE <yara|sigma|suricata|snort>[: <visible name>]
```<language>
<literal body>
```

UNCERTAINTIES
- <uncertainty>

Or, when applicable, return exactly one of these terminal responses:
EMPTY

UNAVAILABLE"""

# Batch output framing marker. The exact markers for a batch are rendered from
# the actual source list below; there is no static B1/B2/B3 example to
# extrapolate. The input side needs no framing: it is a list of exact URLs.
Q2_BATCH_OUTPUT_MARKER = "@@Q2:{batch_id}@@"

_Q2_IOC_RULES_BATCH_BODY_FORMAT = """IOC <confirmed|contextual> <type>
- <value>

RULE <yara|sigma|suricata|snort>[: <visible name>]
```<language>
<literal body>
```

Or, when applicable, return exactly one of these terminal responses:
EMPTY

UNAVAILABLE"""


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
- Keep S1, S2, ... and R1, R2, ... as compact transport aliases only; they
  are not canonical identifiers and must never be presented as such.
- Every event must cite at least one source alias defined above; never cite an
  alias you did not define.
- No date after the research date.
"""

    TECHNICAL_EXTRACTION_MARKDOWN_V1 = (
        """You are analysing one specific CTI source for a reusable, source-centric extraction.

**Source title**: {source_title}

{source_access}

**Output format** — plain Markdown, no outer code fence, no JSON. Use only this
wire format:

"""
        + _Q2_WIRE_FORMAT
        + """

Rules:
- The response is bound to this one source. Do not emit source ids, provenance,
  evidence quotes, model run ids or other internal identifiers. Do not repeat
  the input source URL merely as provenance.
- Emit source-supported facts about malware, tools, files, TTPs, infrastructure,
  victims and campaign context only in non-empty FACT groups. FACT categories
  are exactly: actors, campaigns, malware, tools, infection_chain, ttps,
  victimology, protocols, infrastructure, files, commands, persistence,
  detections, other_technical.
- IOC types are exactly: domain, ip, url, email, md5, sha1, sha256, sha512,
  filename, filepath, cve. `confirmed` means confirmed IOC and `contextual`
  means contextual IOC.
- The complete header is authoritative and self-contained. Do not rely on a
  previous header and do not repeat category, status or type on value lines.
- Emit only values literally visible in this source. This restriction does not
  apply to IOC values: extract URL indicators normally when they are actually
  published by this source.
- Use `:: short context` on FACT values only when useful, with whitespace on
  both sides of `::`. IOC value lines carry no annotation. Keep every IPv6
  literal intact.
- Perform an exhaustive IOC pass: IPv4/IPv6, domains, URLs, MD5/SHA1/SHA256/
  SHA512 and email addresses, including tables, appendices, images and code.
  Omit irrelevant, example-only, placeholder, masked, truncated, REDACTED or
  FUZZ values; never reconstruct hidden values.
- Put complete literal detection rules visible in this source only in RULE. The
  fence is mandatory.
  Preserve the complete literal body, syntax, visible line breaks and visible
  rule name. Never reconstruct truncated content, invent missing variables,
  repair braces, refang, reformat, flatten, unflatten, merge or transform a
  rule. Report partial/truncated rules in UNCERTAINTIES, never as complete
  rules. A flattened one-line YARA rule stays one line, and `hxxps\\://...`
  stays exactly visible.
- EMPTY means the source was actually analysed and contained nothing relevant.
  UNAVAILABLE means the source could not actually be analysed. Either terminal
  response must be alone except for surrounding whitespace.
"""
    )

    IOC_RULES_EXTRACTION_MARKDOWN_V1 = (
        """You are performing a reusable, source-centric IOC and detection-rule extraction for one CTI source.

**Source title**: {source_title}

{source_access}

This profile emits no FACT group or narrative facts: do not extract FACTS, TTP
narrative, victimology, chronology, campaign context, tooling narrative,
infection chains, or general historical context.

**Output format** — plain Markdown, no outer code fence, no JSON. Use this
wire format:

"""
        + _Q2_IOC_RULES_WIRE_FORMAT
        + """

Rules:
- The response is bound to this one source. Do not emit source ids, provenance,
  evidence quotes, model run ids or other internal identifiers. Do not repeat
  the input source URL merely as provenance.
- Emit only non-empty IOC and RULE groups. IOC types are exactly: domain, ip,
  url, email, md5, sha1, sha256, sha512, filename, filepath, cve. `confirmed`
  means confirmed IOC and `contextual` means contextual IOC.
- The complete header is authoritative and self-contained. Do not rely on a
  previous header and do not repeat status or type on value lines.
- Emit only values literally visible in this source. This restriction does not
  apply to IOC values: extract URL indicators normally when they are actually
  published by this source.
- Never sacrifice IOC coverage to reduce cost. Perform an exhaustive IOC pass:
  IPv4/IPv6, domains, URLs, MD5/SHA1/SHA256/SHA512 and email addresses,
  including tables, appendices, images and code.
  Omit irrelevant, example-only, placeholder, masked, truncated, REDACTED or
  FUZZ values; never reconstruct hidden values. Emit only source-supported
  indicators and meaningful uncertainties.
- IOC value lines carry no annotation. Keep every IPv6 literal intact.
- Put complete literal detection rules visible in this source only in RULE. The
  fence is mandatory.
  Preserve the complete literal body, syntax, visible line breaks and visible
  rule name. Never reconstruct truncated content, invent missing variables,
  repair braces, refang, reformat, flatten, unflatten, merge or transform a
  rule. Report partial/truncated rules in UNCERTAINTIES, never as complete
  rules. A flattened one-line YARA rule stays one line, and `hxxps\\://...`
  stays exactly visible.
- EMPTY means the source was actually analysed and contained nothing relevant.
  UNAVAILABLE means the source could not actually be analysed. Either terminal
  response must be alone except for surrounding whitespace.
"""
    )

    IOC_RULES_BATCH_EXTRACTION_MARKDOWN_V1 = (
        """You are performing an IOC and detection-rule extraction over several independent CTI publications.

Open every exact source URL listed below.

Analyse each publication itself. Inspect the complete accessible rendered
source, including technical tables, code blocks, indicator lists,
appendices/annexes reachable from the publication and visible
images/screenshots when available.

Do not replace a source with unrelated search results and do not use another
publication as evidence for that B#.

Treat every B# independently.

For every B#, emit exactly one output section beginning with its exact
@@Q2:B#@@ marker, alone on its line. The next output marker terminates the
previous section and EOF terminates the last section. Do not emit a terminating
marker.

Never use one publication to interpret or classify another. Never move an IOC or
rule between publications. Emit no FACT and no narrative context. Produce a
compact response. The only provenance labels you may emit are the local B#
labels carried by the output markers. Do not repeat an input source URL as
provenance. Do not emit model ids, internal ids or internal content hashes.

This restriction does not apply to IOC values: extract URL, MD5, SHA1, SHA256
and SHA512 indicators normally when they are actually published by that source.

Use EMPTY only after analysing that publication and finding no IOC or rule. Use
UNAVAILABLE only when that publication could not be analysed. Do not let one
failure suppress the other sources.

Extract every source-supported literal IOC and every complete literal YARA,
Sigma, Suricata or Snort rule. Preserve rule syntax, visible line breaks and
visible names. Never invent, repair, refang, reformat, flatten, merge or
transform a rule. Put partial rules in no RULE block.

Output body grammar, shared by every section:

"""
        + _Q2_IOC_RULES_BATCH_BODY_FORMAT
        + """

IOC types are exactly domain, ip, url, email, md5, sha1, sha256, sha512,
filename, filepath and cve. Mark each IOC confirmed or contextual. Do not add
annotations to value lines. The rule fence is mandatory. The framing is
structural and takes precedence over Markdown fences. Never emit a Q2 output
marker that is not one of the section markers listed below.

Output structure for this batch, with one independent section per source:
{batch_output_structure}

Sources:
{batch_sources}
"""
    )

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

    # Q2 analyses the live publication behind the exact canonical URL. The local
    # archive is a collection snapshot and is never inlined here: its derived
    # text does not represent what the rendered publication actually shows.
    LIVE_SOURCE_ACCESS_V1 = """Open this exact source:
{source_url}

Analyse the source itself. Read the complete accessible page; inspect technical
tables, code blocks, appendices/annexes reachable from the publication and
visible images/screenshots when available. Do not replace it with unrelated
search results or use memory as evidence. If the exact source cannot be
accessed, return `UNAVAILABLE` alone and do not invent an extraction."""

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
            source_access=cls.LIVE_SOURCE_ACCESS_V1.format(source_url=source_url),
        )

    @classmethod
    def get_ioc_rules_batch_prompt(
        cls,
        batch_sources: Sequence[tuple[str, str]],
    ) -> str:
        """Render a URL-only IOC_RULES batch using local B# labels."""
        blocks = "\n".join(f"{batch_id} {source_url}" for batch_id, source_url in batch_sources)
        if not blocks.strip():
            raise ValueError("A Q2 batch prompt requires at least one source")
        output_structure = "\n\n".join(
            f"{Q2_BATCH_OUTPUT_MARKER.format(batch_id=batch_id)}\n"
            "<source-local IOC/rule output, EMPTY or UNAVAILABLE>"
            for batch_id, _ in batch_sources
        )
        return cls.IOC_RULES_BATCH_EXTRACTION_MARKDOWN_V1.format(
            batch_sources=blocks,
            batch_output_structure=output_structure,
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
