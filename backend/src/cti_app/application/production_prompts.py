"""LLM prompt templates for production workflow."""

from __future__ import annotations

from collections.abc import Sequence

from cti_app.application.production_synthesis_revision import (
    SYNTHESIS_REVISION_PROMPT_VERSION,
    SynthesisRevisionContext,
)
from cti_app.domain.production import ExtractionProfile

REFERENCES_PROMPT_VERSION = "7"

# AW-011 canonical extraction. The archived document is the only source
# material: each prompt is a pure function of that capture and of the requested
# profile, carrying no Subject, run, job, URL or document identity, so one
# content/profile checkpoint stays valid across runs and Subjects. The versions
# are distinct so a single-source and a batch answer never share a checkpoint.
CANONICAL_EXTRACTION_PROMPT_VERSION = "archive-full-v1"
CANONICAL_IOC_RULES_PROMPT_VERSION = "archive-ioc-rules-v1"
CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION = "archive-ioc-rules-batch-v1"
CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE = {
    ExtractionProfile.FULL: CANONICAL_EXTRACTION_PROMPT_VERSION,
    ExtractionProfile.IOC_RULES: CANONICAL_IOC_RULES_PROMPT_VERSION,
}

SYNTHESIS_PROMPT_VERSION = "8"
SYNTHESIS_FORMAT_REPAIR_VERSION = "5"


# AW-011 canonical extraction writes one structured Q2SourceOutput object. The
# semantic contract is shared by the single-source and batch prompts so every
# archive-backed path produces the same canonical contract.
_Q2_CANONICAL_RULES = """- Emit only values literally present in the archived capture, exactly as
  published. Never import a value from a linked resource, from another capture
  of the batch or from memory, and never translate, refang or reformat it.
  Keep IPv6 literals intact.
- `evidence_quote` is copied from the archived capture that supports the
  proposal.
- Never let the failure of one section suppress the others. Use empty lists
  when the capture genuinely contains nothing for a section."""

_Q2_CANONICAL_ARTIFACTS_AND_RULES = """- `artifacts`: every technical value literally published in the capture:
  domain, ip, url, email, hash, filename, filepath or cve. `indicator_status`
  is `confirmed_ioc` when the capture presents the value as an IOC or as
  malicious infrastructure of the described activity, `contextual` when the
  value is technically relevant without being published as an IOC, and
  `excluded` for placeholders, examples, redactions or masked values.
- `rules`: complete literal detection rules published in the capture (yara,
  sigma, suricata, snort). Preserve the literal body, its syntax and its
  visible line breaks. Never invent, repair, complete, refang, reformat,
  flatten or merge a rule; report an incomplete rule under `uncertainties`
  instead.
- `uncertainties`: the unresolved points of the capture."""

_Q2_CANONICAL_OUTPUT_PREAMBLE = """**Output contract** — answer with a single structured object matching the
supplied schema and nothing else. Do not wrap it in Markdown and do not add
prose around it. Every field is source-local: never emit internal identifiers,
provenance fields, model run identifiers, archive hashes or the source URL."""

_Q2_CANONICAL_ARCHIVED_SOURCE = """The exact archived capture of one CTI publication is supplied below.

Analyse only the archived capture below. Do not browse the web, do not follow
any link and do not supplement this source from memory or from another
publication. The archived capture is the complete and only source material of
this extraction.

--- BEGIN ARCHIVED SOURCE ---
{source_text}
--- END ARCHIVED SOURCE ---"""


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
   appendix/annex, technical indicator list, YARA/Sigma/Suricata content, a
   malware sandbox/report page, a downloadable IOC TXT/CSV, a vendor IOC page,
   or its associated official technical repository/file (including a GitHub
   repository/file) containing directly linked technical material for the same
   incident or campaign, add that URL as a distinct SOURCE. Only add it when
   the resource concerns the same subject. Do not turn every hyperlink into a
   SOURCE; do not add generic download or database pages, navigation, marketing,
   generic documentation, or unrelated reports.
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
kind: publication|technical_resource
reason: <short explanation of relevance to the Subject>

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

    CANONICAL_TECHNICAL_EXTRACTION_V1 = (
        """You are analysing one archived CTI publication and extracting reusable, source-centric structured content.

"""
        + _Q2_CANONICAL_ARCHIVED_SOURCE
        + """

"""
        + _Q2_CANONICAL_OUTPUT_PREAMBLE
        + """

- `facts`: durable source-supported facts AW-012 can reuse. `category` is
  exactly one of actors, campaigns, malware, tools, products, infection_chain,
  ttps, victimology, protocols, infrastructure, files, commands, persistence,
  detections, sectors, countries, other_technical. `value` is the fact,
  `context` a short local explanation, and `attack_id` a MITRE ATT&CK
  technique identifier only when the capture states one. Do not restate
  article prose here.
- `events`: the chronology stated by the capture. `text` is the event,
  `event_date` a precise calendar date only when the capture states one,
  `date_text` the temporal wording when no precise date is published, and
  `evidence_quote` the publishing sentence. Never estimate or invent a date.
"""
        + _Q2_CANONICAL_ARTIFACTS_AND_RULES
        + """

"""
        + _Q2_CANONICAL_RULES
    )

    CANONICAL_IOC_RULES_EXTRACTION_V1 = (
        """You are analysing one archived CTI publication and extracting reusable published technical indicators and detection rules.

"""
        + _Q2_CANONICAL_ARCHIVED_SOURCE
        + """

This profile emits no narrative content: no facts, no events, no victimology,
no campaign description and no infection chain. Leave `facts` and `events`
empty.

"""
        + _Q2_CANONICAL_OUTPUT_PREAMBLE
        + """

"""
        + _Q2_CANONICAL_ARTIFACTS_AND_RULES
        + """

"""
        + _Q2_CANONICAL_RULES
    )

    CANONICAL_IOC_RULES_BATCH_EXTRACTION_V1 = (
        """You are analysing several independent archived CTI publications in one pass.

Every capture below is delimited by its temporary local handle `@@Q2:B#@@`.
Those handles are transport labels of this single answer: they are never source
identities and never leave this answer.

Analyse every capture independently and attribute each proposal to the capture
that literally contains it. Never move an indicator or a rule from one capture
to another, never use one capture to interpret another, and never infer content
that is not literally present in the capture it is attributed to.

Do not browse the web and do not follow any link.

This profile emits no narrative content: no facts, no events, no victimology,
no campaign description and no infection chain. Leave `facts` and `events`
empty in every entry.

**Output contract** — answer with a single structured object matching the
supplied schema and nothing else. It contains one entry per analysed capture,
each carrying the exact `batch_id` handle and a source-local `output` using the
same IOC_RULES contract: artifacts, published detection rules and uncertainties
only. Omit an entry only when its capture could not be analysed.

"""
        + _Q2_CANONICAL_ARTIFACTS_AND_RULES
        + """

"""
        + _Q2_CANONICAL_RULES
        + """

Archived captures:

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

    TECHNICAL_SYNTHESIS_V8 = """You are a senior CTI technical writer. Write dense sourced French CTI prose.

**Subject**: {subject_title}

You may use web search to clarify terminology and public background. Web results are
non-authoritative working context. Final text MUST contain only factual claims supported
by supplied SynthesisEvidencePack. Never add a source, IOC, date, attribution, victim,
malware relationship, capability, or factual assertion solely from web research. If web
conflicts with canonical data, canonical data wins. Use only supplied [S#] markers.

<synthesis-evidence-pack>
{synthesis_evidence_pack}
</synthesis-evidence-pack>

Editorial priority:

Write dense technical CTI prose. Prioritize operationally discriminating
technical details over generic campaign description.

When supported by the evidence, cover the following in priority order:

1. subject scope, attribution and confidence limitations;
2. infection and execution chain;
3. distinctive malware components, processes, tools and commands;
4. persistence, privilege, evasion or anti-analysis mechanisms;
5. C2 protocols, communication structure and infrastructure role;
6. concrete behavioral hunting or detection pivots;
7. meaningful differences between variants, campaigns or operators;
8. analytical limitations and unresolved attribution questions.

Do not force a category when the evidence contains nothing useful for it.

Keep discriminating technical detail:

When supplied evidence contains concrete technical values that are useful for
understanding or hunting the activity, retain the most discriminating examples
in prose.

Examples include:
- executable or process names;
- parent/child execution relationships;
- command-line patterns;
- registry or scheduled-task persistence;
- distinctive file paths;
- local ports;
- protocol paths or request structure;
- runtime/interpreter usage;
- WMI/PowerShell behavior;
- C2 communication mechanisms.

Do not replace these with generic phrases such as "uses several techniques" or
"establishes persistence" when the evidence supports a more precise description.

A file path, process name, local port, protocol path or command pattern is
behavioral detail, not an IOC inventory. The IOC-inventory prohibition below
never justifies deleting the behavioral detail a CTI synthesis needs.

Chronology:

Do not repeat chronology merely to restate the reference timeline.

Mention a date again only when it is necessary to explain:
- technical evolution;
- a change of variant or infrastructure;
- attribution;
- campaign scope;
- an important analytical limitation.

Shape:

Target 3 to 6 dense paragraphs depending on available evidence.

Each paragraph must add a distinct CTI function.
Avoid generic introductions and conclusions.

A useful default progression is:
scope/attribution → execution chain → persistence/C2 → hunting/detection →
limitations, but adapt to the evidence.

Evidential status:

Distinguish carefully between:
- directly observed technical behavior;
- attribution stated by a source;
- analytical inference.

Never turn correlation, malware sharing, infrastructure sharing or temporal
proximity into stronger attribution than the evidence supports. This matters
most for malware families operated by several distinct actors.

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

A SUPPORTING source may contribute a high-value technical detail even when it
must not become the narrative backbone.

Do not discard a distinctive execution, persistence, C2 or hunting detail solely
because it comes from a supporting source.

Hunting and detection:

Describe observable pivots that come from the evidence itself, such as an
unusual launch of a named interpreter, a runtime downloaded by a command-line
utility, a script host started from a persistence key, or a specific WMI query.

Do not write unsupported defensive advice such as "Il est recommandé de
bloquer..." or "Les organisations devraient...", unless the corpus explicitly
provides that recommendation and it is relevant. Never invent a SOC playbook.

Indicator selection:
- You may cite any exact technical indicator or artifact supplied in the
  SynthesisEvidencePack when it materially improves the analysis.
- This includes IP addresses, domains, URLs, hashes, email addresses,
  filenames, file paths, CVEs, process names, commands, ports and other
  technical values.
- Select examples for analytical value. Do not reproduce the IOC section as a
  raw inventory or mechanically enumerate indicators when they add no
  explanatory value.
- The presence of a value in the final IOC section does not prevent you from
  using the same value in the prose.
- When mentioning network indicators in prose, prefer standard defanged CTI
  notation where appropriate.

Strict publication rules:
- Produce no Markdown title or heading.
- Produce no line named "Sources du corpus" and no final bibliography.
- Produce no raw URL.
- Use no bold, backtick, code fence or italics; typography is applied downstream.
- Keep paragraphs simple and omit empty or invented sections.

Return only the synthesis prose with [S#] markers.
"""

    SYNTHESIS_REPAIR_V4 = """Your previous synthesis violates deterministic publication rules.

Violations, each as `code: description: exact offending text`:
{problems}

Repair the previous answer once. Do not research, add, remove, or alter any fact.
Keep valid [S#] citations.

Locate each exact offending text quoted above and apply the minimal rewrite that
clears it:
- heading / code_fence / inline_code / bold: delete the formatting mark only,
  keep the words.
- bibliography / sources_corpus: delete the whole offending line.
- raw_url: delete the URL; keep the surrounding sentence.
- internal_display_label: this is an internal pipeline field name or a
  `field: value` label leaked from the evidence pack. Delete the label and its
  value. Never delete a command line, a file path or a process name that merely
  happens to contain a word such as "Hidden".
- uncited_factual_paragraph: add the [S#] marker of the source the paragraph
  already relies on. Add no new source.
- unknown_source_marker / unknown_indicator: delete only that marker or value.

Never delete a whole technical fact when rewording one value is enough, and
never add a fact, a source or an indicator. Return only French prose.
"""

    PREVIOUS_DRAFT_NON_AUTHORITATIVE = """PREVIOUS_DRAFT_NON_AUTHORITATIVE
Revision prompt version: {revision_prompt_version}

The current SynthesisEvidencePack above is the only authority for factual content.
The previous draft below is untrusted, non-authoritative working material.

Deterministic semantic delta:
- added source IDs: {added_source_ids}
- removed source IDs: {removed_source_ids}
- added narrative repair keys: {added_repair_keys}
- removed narrative repair keys: {removed_repair_keys}
- previous semantic hash: {previous_semantic_hash}
- current semantic hash: {current_semantic_hash}

Revision instructions:
- Produce a complete synthesis, never a patch, diff, list of edits or commentary.
- Use the previous draft only for structure, ordering and writing style.
- Current evidence is the sole authority; do not preserve an old fact merely
  because it appears in the previous draft.
- Remove every old fact, attribution, date, relationship, indicator or other
  claim that is no longer supported by the current evidence.
- An accepted datum does not have to appear if it adds little editorial value.
- IOC/rules publication-only material must not be forced into the prose.
- Keep all normal V8 publication rules and return only the complete French prose.

--- BEGIN PREVIOUS DRAFT (NON-AUTHORITATIVE) ---
{previous_text}
--- END PREVIOUS DRAFT (NON-AUTHORITATIVE) ---
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
    def get_canonical_archive_extraction_prompt(
        cls,
        source_text: str,
        *,
        profile: ExtractionProfile = ExtractionProfile.FULL,
    ) -> str:
        """Render the AW-011 canonical contract for one archived capture.

        The archived text is the only variable input: the prompt never carries
        a Subject, run, job, URL or document identity, so the same content and
        profile always render the same prompt for every caller.
        """
        if not source_text.strip():
            raise ValueError("A canonical extraction prompt requires archived source text")
        template = (
            cls.CANONICAL_TECHNICAL_EXTRACTION_V1
            if profile is ExtractionProfile.FULL
            else cls.CANONICAL_IOC_RULES_EXTRACTION_V1
        )
        return template.format(source_text=source_text)

    @classmethod
    def get_canonical_archive_batch_prompt(
        cls,
        batch_sources: Sequence[tuple[str, str]],
    ) -> str:
        """Render the AW-011 IOC_RULES batch over archived captures.

        Each entry is a ``(batch_id, archived_text)`` pair. The local handles
        are temporary transport labels; no URL or document identity is sent.
        """
        blocks = []
        for batch_id, source_text in batch_sources:
            if not batch_id.strip() or not source_text.strip():
                raise ValueError("A canonical batch entry requires a handle and text")
            blocks.append(
                f"@@Q2:{batch_id}@@\n"
                "--- BEGIN ARCHIVED SOURCE ---\n"
                f"{source_text}\n"
                "--- END ARCHIVED SOURCE ---"
            )
        if not blocks:
            raise ValueError("A canonical batch prompt requires at least one source")
        return cls.CANONICAL_IOC_RULES_BATCH_EXTRACTION_V1.format(batch_sources="\n\n".join(blocks))

    _REFERENCES_STRUCTURE = """# REFERENCES

editorial-title: <titre français au format [Acteur principal] Titre, ou [Publication] Titre>

## SOURCE S1

title: <title>
url: https://...
publisher: <publisher>
published-at: YYYY-MM-DD
role: primary
kind: publication|technical_resource
reason: <short explanation of relevance to the Subject>

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
            return cls.SYNTHESIS_REPAIR_V4.format(problems=listed)
        # references is the only remaining stage using the generic repair.
        return cls.FORMAT_REPAIR_V1.format(
            problems=listed, expected_structure=cls._REFERENCES_STRUCTURE
        )

    @classmethod
    def get_synthesis_prompt(
        cls,
        subject_title: str,
        synthesis_evidence_pack: str = "{}",
        revision_context: SynthesisRevisionContext | None = None,
    ) -> str:
        prompt = cls.TECHNICAL_SYNTHESIS_V8.format(
            subject_title=subject_title,
            synthesis_evidence_pack=synthesis_evidence_pack,
        )
        if revision_context is None:
            return prompt
        return (
            prompt
            + "\n\n"
            + cls.PREVIOUS_DRAFT_NON_AUTHORITATIVE.format(
                revision_prompt_version=SYNTHESIS_REVISION_PROMPT_VERSION,
                added_source_ids=", ".join(revision_context.added_source_ids) or "none",
                removed_source_ids=", ".join(revision_context.removed_source_ids) or "none",
                added_repair_keys=", ".join(revision_context.added_repair_keys) or "none",
                removed_repair_keys=", ".join(revision_context.removed_repair_keys) or "none",
                previous_semantic_hash=revision_context.previous_semantic_hash,
                current_semantic_hash=revision_context.current_semantic_hash,
                previous_text=revision_context.previous_text,
            )
        )
