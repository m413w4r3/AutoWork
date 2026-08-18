"""LLM prompt templates for production workflow."""

from __future__ import annotations

from collections.abc import Sequence


class ProductionPromptTemplates:
    """Versioned prompt templates for subject production."""

    REFERENCES_RESEARCH_V1 = """You are a threat intelligence research assistant. Your task is to conduct web research and build a chronological reference timeline for the following subject:

**Subject**: {subject_title}

**Initial Information**:
{subject_description}

**Actor/Campaign**: {actor_info}

**Technical Data**:
{technical_summary}

**Research Date**: {research_date}

**Editorial Period**: {period_start} to {period_end}

**Known Publications**:
{existing_sources}

**Research Guidelines**:
1. Prioritize government bodies (CISA, CERT, national agencies)
2. Technical sources from original researchers and security vendors
3. Independent technical analysis and published research
4. Avoid redundant reprints without added value
5. Verify all dates are on or before the research date
6. Use only publicly available information

**Output format** — plain Markdown, no code fence, no JSON:

# REFERENCES

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
- One `## SOURCE` block per publication, numbered S1, S2, ...
- One `## EVENT` block per dated event, numbered R1, R2, ...
- Every event must cite at least one source id you defined above.
- Never cite a source id you did not define.
- No date after the research date.
"""

    TECHNICAL_EXTRACTION_V1 = """You are a CTI analysis assistant. Extract the technical intelligence contained in the references you produced in this conversation.

**Subject**: {subject_title}

**Rules**:
1. Use ONLY the references and sources established earlier in this conversation.
2. Never invent an indicator, a hash, a domain or a CVE.
3. Every item must cite the events (R#) and/or sources (S#) that support it.
4. Omit a category entirely, or write `none`, when the subject has nothing for it.

**Output format** — plain Markdown, no code fence, no JSON:

# EXTRACTION CTI

## ACTORS
### ITEM A1
value: <value>
context: <short context, in French>
references: R1
sources: S1

## TTP
### ITEM T1
value: <technique name>
attack-id: T1566.001
context: <context>
references: R1
sources: S1

## NETWORK ARTIFACTS
### ITEM N1
type: domain|ip|url|hash|email
value: <value>
context: <context>
references: R1
sources: S1

## PERSISTENCE
none

# UNCERTAINTIES
- <uncertainty>

Available categories: ACTORS, CAMPAIGNS, VICTIMOLOGY, INFECTION CHAIN, MALWARE,
TOOLS, TTP, CVE, PROTOCOLS, NETWORK ARTIFACTS, INFRASTRUCTURE, FILES, COMMANDS,
PERSISTENCE, DETECTIONS, OTHER TECHNICAL.
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

    TECHNICAL_SYNTHESIS_V1 = """You are a technical writer for threat intelligence reports. Your task is to synthesize research into a comprehensive technical briefing.

**Subject**: {subject_title}

**Available Data**:
- Reference timeline (provided above)
- Technical extraction (provided above)
- Supporting IOC indicators

**Writing Guidelines**:
1. Write in French, without internal section headings
2. Use ONLY information from the references and extraction
3. DO NOT add new sources or conduct research
4. DO NOT add new IOC or assertions not in extraction
5. Mark source references with [S1], [S2], etc. using source IDs
6. Each factual claim must have source attribution
7. Group paragraphs by topic when logical, but keep structure simple

**Content Order (if material available)**:
1. Brief presentation of the activity
2. Threat actor / attribution / context
3. Victimology (targets)
4. Infection chain / operational flow
5. Malware and tools
6. Technical protocols, network, infrastructure
7. Other technical elements
8. Limitations and uncertainties (if needed)

**Rules**:
- No empty sections
- No invented paragraphs to fill structure
- [S#] markers must refer to actual sources
- No external URLs not in the source bundle
- Technical assertions must match extracted IOCs

**Output**: Return markdown text (no heading, no code blocks). Include [S#] markers for each source reference.

Example of good synthesis:
Le groupe Exemple réalise depuis 2020 des attaques ciblées contre le secteur financier [S1]. Les campagnes utilisent des emails de phishing contenant des pièces jointes malveillantes [S2]. La chaîne d'infection commence par un document Office piégé [S1], suivi du téléchargement du malware AwesomeMalware [S3] via C2 situé sur infrastructure Telia [S2]. Les IOC associés incluent l'adresse 192.0.2.1 [S1] et le domaine malicious.example.com [S3].
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
        existing_sources_text: str,
    ) -> str:
        """Generate references research prompt."""
        return cls.REFERENCES_RESEARCH_V1.format(
            subject_title=subject_title,
            subject_description=subject_description,
            actor_info=actor_info,
            technical_summary=technical_summary,
            research_date=research_date,
            period_start=period_start,
            period_end=period_end,
            existing_sources=existing_sources_text,
        )

    @classmethod
    def get_extraction_prompt(
        cls,
        subject_title: str,
    ) -> str:
        """Generate CTI extraction prompt."""
        return cls.TECHNICAL_EXTRACTION_V1.format(
            subject_title=subject_title,
        )

    _REFERENCES_STRUCTURE = """# REFERENCES

## SOURCE S1

title: <title>
url: https://...
publisher: <publisher>
published-at: YYYY-MM-DD
role: primary

## EVENT R1

date: YYYY-MM-DD
sources: S1
text: <event>"""

    _EXTRACTION_STRUCTURE = """# EXTRACTION CTI

## ACTORS
### ITEM A1
value: <value>
context: <context>
references: R1
sources: S1"""

    @classmethod
    def get_format_repair_prompt(cls, *, stage: str, problems: Sequence[str]) -> str:
        """Ask the model to restructure its previous answer, nothing else."""
        structure = (
            cls._REFERENCES_STRUCTURE if stage == "references" else cls._EXTRACTION_STRUCTURE
        )
        listed = "\n".join(f"- {problem}" for problem in problems) or "- structure illisible"
        return cls.FORMAT_REPAIR_V1.format(problems=listed, expected_structure=structure)

    @classmethod
    def get_synthesis_prompt(
        cls,
        subject_title: str,
    ) -> str:
        """Generate technical synthesis prompt."""
        return cls.TECHNICAL_SYNTHESIS_V1.format(
            subject_title=subject_title,
        )
