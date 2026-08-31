"""LLM prompt templates for production workflow."""

from __future__ import annotations

from collections.abc import Sequence

from cti_app.domain.production import ExtractionProfile

REFERENCES_PROMPT_VERSION = "4"
# Q2 uses free-text GPT plus a compact grouped Markdown parser. The bridge does
# not guarantee response_format / JSON Schema.
# "11" / "4": Q2 analyses the archived capture inlined in the prompt whenever
# one is available, so a result cached under a content hash is really produced
# from that content. Versions bumped so checkpoints written by the previous,
# live-URL-only prompt are never reused under the same identity.
EXTRACTION_PROMPT_VERSION = "11"
IOC_RULES_PROMPT_VERSION = "4"
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

    TECHNICAL_EXTRACTION_MARKDOWN_V1 = """You are analysing one specific CTI source for a reusable, source-centric extraction.

**Source title**: {source_title}

{source_access}

Perform an exhaustive IOC pass: IPv4/IPv6, domains, URLs, MD5/SHA1/SHA256/
SHA512 and email addresses, including tables, appendices, images and code.
Also extract useful malware, tools, files, CVEs, detection rules, TTPs, infrastructure,
victims and campaign context. Omit irrelevant, example-only, placeholder,
masked, truncated, REDACTED or FUZZ values; never reconstruct hidden values.

Extract the complete literal detection rule when it is visibly published by the
exact source or its explicitly linked technical annex/repository already admitted
to the source corpus. For every YARA, Sigma, Suricata or Snort rule:
- preserve the complete literal body, syntax and visible line breaks;
- do not reconstruct truncated content, invent missing variables, or repair braces;
- never merge two rules or transform one rule language into another;
- preserve the visible rule name;
- report partial/truncated rules in UNCERTAINTIES instead of promoting them as complete;
- a flattened one-line YARA rule stays one line, and `hxxps\\://...` stays exactly visible.

**Output format** — plain Markdown, no outer code fence, no JSON. Use this
compact grouped dialect exactly:

# FACTS

## <category>
- <value>
- <value> :: <short useful context, only when needed>

Categories: actors, campaigns, malware, tools, infection_chain, ttps,
victimology, protocols, infrastructure, files, commands, persistence,
detections, other_technical.

# IOCS

## confirmed
domain:
- <confirmed domain>
ip:
- <confirmed IP>
url:
- <confirmed URL>
email:
- <confirmed email>
md5:
- <confirmed MD5>
sha1:
- <confirmed SHA1>
sha256:
- <confirmed SHA256>
sha512:
- <confirmed SHA512>
filename:
- <confirmed filename>
filepath:
- <confirmed filepath>
cve:
- <confirmed CVE>

## contextual
domain:
- <contextual value> :: <short useful context, only when needed>
cve:
- <contextual CVE>

# RULES

## yara: <visible name, omit the colon/name if none>
```yara
<complete literal rule body, preserving exactly what is visible>
```

## sigma: <visible name, omit the colon/name if none>
```yaml
<complete literal rule body, preserving exactly what is visible>
```

## suricata: <visible name, omit the colon/name if none>
```suricata
<complete literal rule body, preserving exactly what is visible>
```

## snort: <visible name, omit the colon/name if none>
```snort
<complete literal rule body, preserving exactly what is visible>
```

# UNCERTAINTIES
* <only meaningful uncertainty, or omit the section>

Rules:
- The response is bound to this one source. Never repeat its URL, source id,
  provenance, evidence quote, model run id, or other internal identifier.
- `confirmed` means confirmed_ioc and `contextual` means contextual. Never
  emit excluded or not_applicable artifacts; omit unusable values instead.
- The group header supplies category, IOC type and status. Do not repeat those
  fields on each value. Do not emit evidence quotes or repeated context.
- Emit only values literally visible in this source or its admitted technical
  annex. A value may have one short `:: context` annotation when genuinely useful.
- Put complete literal detection rules only in RULES. Never refang, reformat,
  flatten, unflatten, repair, or reconstruct a rule body.
"""

    IOC_RULES_EXTRACTION_MARKDOWN_V1 = """You are performing a reusable, source-centric IOC and detection-rule extraction for one CTI source.

**Source title**: {source_title}

{source_access}

Never sacrifice IOC coverage to reduce cost. This profile emits no narrative
facts: do not extract FACTS, TTP narrative, victimology, chronology, campaign
context, tooling narrative, infection chains, or general historical context.

Extract the complete literal detection rule when it is visibly published by the
exact source or its explicitly linked technical annex/repository already admitted
to the source corpus. For every YARA, Sigma, Suricata or Snort rule:
- preserve the complete literal body, syntax and visible line breaks;
- do not reconstruct truncated content, invent missing variables, or repair braces;
- never merge two rules or transform one rule language into another;
- preserve the visible rule name;
- report partial/truncated rules in UNCERTAINTIES instead of promoting them as complete;
- a flattened one-line YARA rule stays one line, and `hxxps\\://...` stays exactly visible.

Extract only every source-supported IOC, indicator file, relevant CVE,
complete detection rule, and meaningful uncertainty. Omit irrelevant,
example-only, placeholder, masked, truncated, REDACTED or FUZZ values.

**Output format** — plain Markdown, no outer code fence, no JSON. Use only:

# IOCS

## confirmed
domain:
- <confirmed domain>
ip:
- <confirmed IP>
url:
- <confirmed URL>
email:
- <confirmed email>
md5:
- <confirmed MD5>
sha1:
- <confirmed SHA1>
sha256:
- <confirmed SHA256>
sha512:
- <confirmed SHA512>
filename:
- <confirmed filename>
filepath:
- <confirmed filepath>
cve:
- <confirmed CVE>

## contextual
domain:
- <contextual value> :: <short useful context, only when needed>
cve:
- <contextual CVE>

# RULES

## yara: <visible name, omit the colon/name if none>
```yara
<complete literal rule body, preserving exactly what is visible>
```

## sigma: <visible name, omit the colon/name if none>
```yaml
<complete literal rule body, preserving exactly what is visible>
```

## suricata: <visible name, omit the colon/name if none>
```suricata
<complete literal rule body, preserving exactly what is visible>
```

## snort: <visible name, omit the colon/name if none>
```snort
<complete literal rule body, preserving exactly what is visible>
```

# UNCERTAINTIES
* <only meaningful uncertainty, or omit the section>

Rules:
- The response is bound to this one source. Never repeat its URL, source id,
  provenance, evidence quote, model run id, or other internal identifier.
- `confirmed` means confirmed_ioc and `contextual` means contextual. Never
  emit excluded or not_applicable artifacts; omit unusable values instead.
- The group header supplies IOC type and status. Do not repeat those fields on
  each value. Do not emit FACTS or narrative/context extraction.
- A value may have one short `:: context` annotation only when genuinely useful.
- Put complete literal detection rules only in RULES. Never refang, reformat,
  flatten, unflatten, repair, or reconstruct a rule body.
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

    # No archived capture is usable: the model has to open the live page, and
    # the result stays subject-local (never content-addressed), because what it
    # actually read cannot be tied to any content hash we hold.
    LIVE_SOURCE_ACCESS_V1 = """Open this exact source:
{source_url}

Analyse the source itself. Read the complete accessible page; inspect technical
tables, code blocks and visible images/screenshots when available. Do not
replace it with unrelated search results or use memory as evidence. If the
exact source cannot be accessed, report `source_unavailable` in UNCERTAINTIES
and do not invent an extraction."""

    # The archived capture is the analysed document, so the extraction really is
    # a function of the content hash it is cached under.
    ARCHIVED_SOURCE_ACCESS_V1 = """Source URL (context and provenance only):
{source_url}

The archived capture of this exact source is reproduced below, between the
`<ARCHIVED_SOURCE>` markers. That archived content is the document to analyse:
read it completely, including technical tables, code blocks, appendices/annexes
and indicator lists. Do not replace it with the current live page, unrelated
search results, or memory. Web access may only help you interpret what the
archived content already contains; never add a fact, indicator or rule that is
absent from it. If the archived content is unusable, report `source_unavailable`
in UNCERTAINTIES and do not invent an extraction.

Archived source content:

<ARCHIVED_SOURCE>
{archived_source_content}
</ARCHIVED_SOURCE>"""

    @classmethod
    def get_extraction_prompt(
        cls,
        subject_title: str,
        source_id: str = "",
        source_title: str = "",
        source_url: str = "",
        profile: ExtractionProfile = ExtractionProfile.FULL,
        archived_source_content: str | None = None,
    ) -> str:
        del subject_title, source_id
        template = (
            cls.TECHNICAL_EXTRACTION_MARKDOWN_V1
            if profile is ExtractionProfile.FULL
            else cls.IOC_RULES_EXTRACTION_MARKDOWN_V1
        )
        source_access = (
            cls.LIVE_SOURCE_ACCESS_V1.format(source_url=source_url)
            if archived_source_content is None
            else cls.ARCHIVED_SOURCE_ACCESS_V1.format(
                source_url=source_url,
                archived_source_content=archived_source_content,
            )
        )
        return template.format(
            source_title=source_title,
            source_url=source_url,
            source_access=source_access,
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
