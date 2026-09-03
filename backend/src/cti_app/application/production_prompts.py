"""LLM prompt templates for production workflow."""

from __future__ import annotations

from collections.abc import Sequence

from cti_app.domain.production import ExtractionProfile

REFERENCES_PROMPT_VERSION = "6"
# Q2 uses free-text GPT plus a stateless Markdown wire-format parser. The bridge does
# not guarantee response_format / JSON Schema.
# "18" / "11": Q2 analyses the live publication behind the exact canonical URL,
# including its rendered tables, code and visible images. The local archive is
# collection provenance and is never inlined in the prompt. Extraction is now
# bounded by the requested Subject: exhaustiveness applies to subject-relevant
# IOCs/rules, not to every indicator visible in a multi-actor publication.
EXTRACTION_PROMPT_VERSION = "18"
IOC_RULES_PROMPT_VERSION = "11"
# Archive fallback is a separate access contract. The profile-specific Q2
# contract remains the same; only the source-access block and its identity
# change.
ARCHIVED_SOURCE_ACCESS_VERSION = "1"
ARCHIVE_FALLBACK_PROMPT_VERSION = "1"
# "10": the batch input is the compact list of exact source URLs plus the single
# shared Subject. Only the output stays marker-framed: a marker starts the next
# block; EOF closes the final block.
IOC_RULES_BATCH_PROMPT_VERSION = "10"
EXTRACTION_PROMPT_VERSION_BY_PROFILE = {
    ExtractionProfile.FULL: EXTRACTION_PROMPT_VERSION,
    ExtractionProfile.IOC_RULES: IOC_RULES_PROMPT_VERSION,
}
SYNTHESIS_PROMPT_VERSION = "8"
REFERENCES_FORMAT_REPAIR_VERSION = "1"
SYNTHESIS_FORMAT_REPAIR_VERSION = "4"


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

# Subject relevance policy shared by the three Q2 extraction paths. Selection
# happens during extraction, while the model still has the full publication in
# context: there is no post-Q2 classification pass and no deterministic actor
# list on the Python side.
_Q2_SUBJECT_RELEVANCE_POLICY = """Subject relevance is mandatory.

Analyse the complete source, but emit only technical facts, IOCs and detection
rules relevant to the requested Subject.

A publication may discuss several actors, campaigns, malware families or
operations. Relevance of the publication does not imply relevance of every
indicator contained in it.

For every IOC, determine from its local source context what actor, campaign,
malware, operation or technical activity it belongs to before emitting it.

Emit an IOC when the source associates it with:
- the requested subject;
- the actor/campaign represented by the requested subject;
- the malware/family central to the requested subject;
- infrastructure or artifacts explicitly supporting that subject.

Do NOT emit an IOC merely because it appears elsewhere in the same publication.

Do NOT emit an IOC when the source explicitly associates it with:
- another actor;
- another campaign or operation;
- another unrelated malware family;
- a comparison or historical-background section unrelated to the subject;
- another row/group of a multi-actor IOC table.

When an IOC's relationship to the subject is ambiguous, do not emit it as
confirmed.

IOC status is about the source's publication and association decision, not your
confidence about the indicator's exact technical function. Use `confirmed` when
the publication explicitly presents a value as an IOC, malicious infrastructure,
C2 infrastructure, malware/file indicator or other defensive indicator belonging
to the requested Subject. If the source places the value in a subject-relevant
block headed `Indicators of Compromise`, `Indicators`, `IOCs`, `C2
infrastructure`, `Malicious infrastructure`, `Network indicators`, `File
indicators`, `Hashes`, `Domains and IPs`, or an IOC appendix/table, preserve it
as `confirmed` even when it says potentially associated infrastructure, the
exact operational role is unknown, or the purpose of the host is uncertain.
Those qualifications concern function, not whether the source published the
value for the Subject.

Use `contextual` only when the technical value is relevant but the source does
not itself establish it as an IOC of the requested Subject. This includes an
example-only narrative mention, historically related infrastructure not tied to
the campaign, a legitimate service domain, a research pivot, an explicitly
hypothetical or ambiguous subject association, or an indicator belonging to a
nearby but distinct activity. Do not promote every value appearing in the
publication to `confirmed`.

Shared legitimate infrastructure is not a useful IOC by itself. Generic roots
or services such as GitHub, Telegram, Google Drive, common cloud platforms,
public CDNs or vendor infrastructure must not be emitted solely because the
subject used the service. A campaign-specific repository, account, URL,
subdomain or other discriminating artifact may be emitted when explicitly
supported.

A detection rule must also be relevant to the requested Subject. Do not emit a
rule explicitly associated only with another actor, campaign, malware family or
operation mentioned in the publication.

Source-local boundary is mandatory. Analyse only material belonging to this
exact source URL: its rendered text, tables, code blocks and visible visual
content that belongs to that page. Do not follow a link to another publication,
IOC page, repository, sandbox report or appendix and attribute its indicators to
this source. Linked technical resources are distinct sources for Q1.

Exhaustiveness applies after relevance filtering: find every subject-relevant
IOC, not every IOC in the publication. Perform an exhaustive subject-relevant
IOC pass: IPv4/IPv6, domains, URLs, MD5/SHA1/SHA256/SHA512 and email addresses,
including rendered tables, visible images and code. Omit irrelevant, example-only,
placeholder, masked, truncated, REDACTED or FUZZ values; never reconstruct
hidden values. Exhaustiveness includes every literal value in a subject-relevant
IOC table or appendix rendered in this exact source: do not sample, summarize,
collapse ranges, omit
repetitive-looking subdomains, or stop after representative examples. A single
table cell may contain multiple IOC literals separated by whitespace, newline,
comma or semicolon. Treat each separator-delimited valid literal as a separate
IOC and emit each one on its own `- <value>` line; never treat the whole cell as
one value. For example, emit `uae1.example` and `uae14.example` separately; do
not replace them with `uae1-uae14.example` or `multiple uae*.example subdomains`.

Never sacrifice coverage of subject-relevant IOCs to reduce cost. Never
increase coverage by importing indicators belonging to other activities
mentioned in the source."""

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

**Subject**: {subject_title}

**Source title**: {source_title}

{source_access}

"""
        + _Q2_SUBJECT_RELEVANCE_POLICY
        + """

**Output format** — plain Markdown, no outer code fence, no JSON. Use only this
wire format:

"""
        + _Q2_WIRE_FORMAT
        + """

Rules:
- The response is bound to this one source. Do not emit source ids, provenance,
  evidence quotes, model run ids or other internal identifiers. Do not repeat
  the input source URL merely as provenance.
- Emit source-supported, subject-relevant facts about malware, tools, files,
  TTPs, infrastructure, victims and campaign context only in non-empty FACT
  groups. FACT categories are exactly: actors, campaigns, malware, tools,
  infection_chain, ttps, victimology, protocols, infrastructure, files,
  commands, persistence, detections, other_technical.
- Facts about another activity may be emitted only when they materially clarify
  the requested subject's attribution, malware sharing, infrastructure sharing,
  technical relationship or uncertainty. Do not extract unrelated parallel
  activity as standalone subject facts.
- IOC types are exactly: domain, ip, url, email, md5, sha1, sha256, sha512,
  filename, filepath, cve. `confirmed` means confirmed IOC and `contextual`
  means contextual IOC.
- The complete header is authoritative and self-contained. Do not rely on a
  previous header and do not repeat category, status or type on value lines.
- Emit only values literally published or rendered in this exact source page.
  Extract a URL indicator only when this exact page publishes it; never follow
  a linked URL to obtain an indicator.
- Use `:: short context` on FACT values only when useful, with whitespace on
  both sides of `::`. IOC value lines carry no annotation. Keep every IPv6
  literal intact.
- Apply the subject relevance policy above to every IOC and rule, then be
  exhaustive within what it allows.
- Put complete literal detection rules visible in this source, and relevant to
  the requested Subject, only in RULE. The fence is mandatory.
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

**Subject**: {subject_title}

**Source title**: {source_title}

{source_access}

"""
        + _Q2_SUBJECT_RELEVANCE_POLICY
        + """

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
- Emit only values literally published or rendered in this exact source page.
  Extract a URL indicator only when this exact page publishes it; never follow
  a linked URL to obtain an indicator.
- Apply the subject relevance policy above to every IOC and rule, then be
  exhaustive within what it allows. Emit only source-supported indicators and
  meaningful uncertainties.
- IOC value lines carry no annotation: emit the bare value, with no attribution,
  campaign label or justification. Keep every IPv6 literal intact.
- Put complete literal detection rules visible in this source, and relevant to
  the requested Subject, only in RULE. The fence is mandatory.
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

**Subject**: {subject_title}

Open every exact source URL listed below.

Analyse each publication itself. Inspect the complete accessible rendered
material belonging to that exact URL, including technical tables, code blocks,
indicator lists and visible images/screenshots when available.

Do not follow a link to another publication, IOC page, repository, sandbox
report or appendix and attribute its indicators to the current B#. Linked
technical resources must be handled as distinct sources by Q1.

Do not replace a source with unrelated search results and do not use another
publication as evidence for that B#.

Treat every B# independently.

"""
        + _Q2_SUBJECT_RELEVANCE_POLICY
        + """

The Subject is the relevance boundary for every B#. Source independence does not
suspend subject filtering. For each publication independently:
1. inspect the complete publication;
2. identify which IOC/rule groups belong to the requested Subject;
3. discard indicators/rules explicitly belonging to other activities;
4. exhaustively emit the remaining subject-relevant indicators/rules.

For every B#, emit exactly one output section beginning with its exact
@@Q2:B#@@ marker, alone on its line. The next output marker terminates the
previous section and EOF terminates the last section. Do not emit a terminating
marker.

Never use one publication to interpret or classify another. Never move an IOC or
rule between publications. Emit no FACT and no narrative context. Produce a
compact response. The only provenance labels you may emit are the local B#
labels carried by the output markers. Do not repeat an input source URL as
provenance. Do not emit model ids, internal ids or internal content hashes.

Extract URL, MD5, SHA1, SHA256 and SHA512 indicators only when they are
actually published by that exact source URL; never follow a linked resource to
obtain an indicator.

Use EMPTY only after analysing that publication and finding no IOC or rule. Use
UNAVAILABLE only when that publication could not be analysed. Do not let one
failure suppress the other sources.

Extract every subject-relevant source-supported literal IOC and every
subject-relevant complete literal YARA, Sigma, Suricata or Snort rule from that
publication. Preserve rule syntax, visible line breaks and
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

Violations, each as `code: offending detail`:
{problems}

Repair the previous answer once. Do not research, add, remove, or alter any fact.
Keep valid [S#] citations.

Apply the minimal rewrite that clears each listed violation:
- Remove headings, bibliography, raw URLs, "Sources du corpus" lines and
  formatting marks.

Never delete a whole technical fact when rewording one value is enough, and
never add a fact, a source or an indicator. Return only French prose.
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

Analyse only material belonging to this exact source URL. Read the complete
accessible page; inspect its rendered text, technical tables, code blocks and
visible images/screenshots that belong to the page. Do not follow a link to
another publication, IOC page, repository, sandbox report or appendix and
attribute its indicators to this source. Linked technical resources must be
handled as distinct sources by Q1. Do not replace the exact source with
unrelated search results or use memory as evidence. If the exact source cannot
be accessed, return `UNAVAILABLE` alone and do not invent an extraction."""

    ARCHIVED_SOURCE_ACCESS_V1 = """The live source could not be accessed.

Analyse only the archived capture supplied below.

Canonical source URL (provenance only):
{source_url}

Do not browse the web.
Do not follow links.
Do not supplement this source from memory or other publications.
Treat the archived content below as the complete source material available
for this extraction.

--- BEGIN ARCHIVED SOURCE ---
{source_text}
--- END ARCHIVED SOURCE ---"""

    @classmethod
    def get_extraction_prompt(
        cls,
        subject_title: str,
        source_id: str = "",
        source_title: str = "",
        source_url: str = "",
        profile: ExtractionProfile = ExtractionProfile.FULL,
    ) -> str:
        del source_id
        template = (
            cls.TECHNICAL_EXTRACTION_MARKDOWN_V1
            if profile is ExtractionProfile.FULL
            else cls.IOC_RULES_EXTRACTION_MARKDOWN_V1
        )
        return template.format(
            subject_title=subject_title,
            source_title=source_title,
            source_url=source_url,
            source_access=cls.LIVE_SOURCE_ACCESS_V1.format(source_url=source_url),
        )

    @classmethod
    def get_archived_extraction_prompt(
        cls,
        subject_title: str,
        source_id: str = "",
        source_title: str = "",
        source_url: str = "",
        source_text: str = "",
        profile: ExtractionProfile = ExtractionProfile.FULL,
    ) -> str:
        """Render the normal Q2 contract against one supplied archive only."""
        del source_id
        template = (
            cls.TECHNICAL_EXTRACTION_MARKDOWN_V1
            if profile is ExtractionProfile.FULL
            else cls.IOC_RULES_EXTRACTION_MARKDOWN_V1
        )
        return template.format(
            subject_title=subject_title,
            source_title=source_title,
            source_url=source_url,
            source_access=cls.ARCHIVED_SOURCE_ACCESS_V1.format(
                source_url=source_url,
                source_text=source_text,
            ),
        )

    @classmethod
    def get_ioc_rules_batch_prompt(
        cls,
        subject_title: str,
        batch_sources: Sequence[tuple[str, str]],
    ) -> str:
        """Render a URL-only IOC_RULES batch using local B# labels.

        The Subject is stated once for the whole batch: it is the relevance
        boundary shared by every B#, never repeated per source block.
        """
        blocks = "\n".join(f"{batch_id} {source_url}" for batch_id, source_url in batch_sources)
        if not blocks.strip():
            raise ValueError("A Q2 batch prompt requires at least one source")
        output_structure = "\n\n".join(
            f"{Q2_BATCH_OUTPUT_MARKER.format(batch_id=batch_id)}\n"
            "<source-local IOC/rule output, EMPTY or UNAVAILABLE>"
            for batch_id, _ in batch_sources
        )
        return cls.IOC_RULES_BATCH_EXTRACTION_MARKDOWN_V1.format(
            subject_title=subject_title,
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
    ) -> str:
        return cls.TECHNICAL_SYNTHESIS_V8.format(
            subject_title=subject_title,
            synthesis_evidence_pack=synthesis_evidence_pack,
        )
