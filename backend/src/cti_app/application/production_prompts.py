"""LLM prompt templates for production workflow."""
from __future__ import annotations

from typing import Any


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

**Output Format**: Return ONLY a valid JSON object within a single markdown code block (```json ... ```). Do not include any text outside the code block.

Schema:
{{
  "subject": "Brief subject identifier",
  "sources": [
    {{
      "id": "S1",
      "url": "https://...",
      "title": "Source title",
      "publisher": "Organization name",
      "published_at": "YYYY-MM-DD or null",
      "role": "primary|independent|relay|aggregator|social|unknown"
    }}
  ],
  "events": [
    {{
      "id": "R1",
      "date": "YYYY-MM-DD or null",
      "text": "Chronological event in French",
      "source_ids": ["S1", "S2"]
    }}
  ],
  "uncertainties": ["Any uncertainties about the research"]
}}
"""

    TECHNICAL_EXTRACTION_V1 = """You are a CTI (Cyber Threat Intelligence) analysis assistant. Your task is to extract technical intelligence from the provided references.

**Subject**: {subject_title}

**Reference Timeline**: (already provided in previous messages)

**Extraction Guidelines**:
1. Use ONLY information from the reference timeline
2. DO NOT conduct new web research or add new publications
3. For unsupported claims, include in "uncertainties" with explanation
4. Extract maximum technical detail with source attribution
5. Each element must be supported by at least one reference

**Expected CTI Categories**:
- actors: Threat actors, groups, individuals
- campaigns: Named operations and campaigns
- victimology: Target sectors, countries, organizations
- infection_chain: Attack progression steps
- malware: Malware families and variants
- tools: Attacker tools and utilities
- ttps: Techniques (MITRE ATT&CK)
- cves: CVEs exploited or discussed
- protocols: Protocols used in attacks
- network_artifacts: IPs, domains, URLs
- infrastructure: C2 servers, hosting providers
- files: File hashes, names, metadata
- commands: Commands executed
- persistence: Persistence mechanisms
- detections: Detection signatures, indicators
- other_technical: Other relevant technical details
- uncertainties: Claims without sufficient support

**Output Format**: Return ONLY a valid JSON object within a single markdown code block.

Schema:
{{
  "actors": [
    {{
      "value": "Actor name",
      "context": "Brief description",
      "reference_ids": ["R1"],
      "source_ids": ["S1"]
    }}
  ],
  "campaigns": [],
  "victimology": [],
  "infection_chain": [
    {{
      "order": 1,
      "value": "Step description",
      "context": "Details",
      "reference_ids": ["R1"],
      "source_ids": ["S1"]
    }}
  ],
  "malware": [],
  "tools": [],
  "ttps": [
    {{
      "attack_id": "T1234 or null",
      "value": "Technique name",
      "context": "How used",
      "reference_ids": ["R1"],
      "source_ids": ["S1"]
    }}
  ],
  "cves": [],
  "protocols": [],
  "network_artifacts": [
    {{
      "type": "ipv4|ipv6|domain|url|hash|email|hostname|port|uri",
      "value": "The artifact",
      "context": "What it is used for",
      "reference_ids": ["R1"],
      "source_ids": ["S1"]
    }}
  ],
  "infrastructure": [],
  "files": [],
  "commands": [],
  "persistence": [],
  "detections": [],
  "other_technical": [],
  "uncertainties": ["Items not fully supported by references"]
}}
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

    @classmethod
    def get_synthesis_prompt(
        cls,
        subject_title: str,
    ) -> str:
        """Generate technical synthesis prompt."""
        return cls.TECHNICAL_SYNTHESIS_V1.format(
            subject_title=subject_title,
        )
