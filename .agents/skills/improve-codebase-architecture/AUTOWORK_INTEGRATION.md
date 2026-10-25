# AutoWork integration — improve-codebase-architecture

Upstream: https://github.com/mattpocock/skills/tree/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/engineering/improve-codebase-architecture
Pinned source commit: 5beb35b040e3de97e54fa9e386d7e42d15c295c2 (2026-10-25 inspection). The upstream SKILL.md, HTML-REPORT.md and agents/openai.yaml are copied unchanged. Copyright (c) 2026 Matt Pocock; MIT license at ../UPSTREAM-MIT-LICENSE.

## AutoWork local application

- Start with AGENTS.md, the local codebase-design skill, existing ADRs, docs/mission03-architecture/ and docs/mission04-closure/. The repository has no root GLOSSARY.md at the inspected baseline; derive terminology from existing domain documentation, and propose glossary updates only when needed.
- This skill is a *diagnostic*: read recent commits, inspect hot modules, create a before/after HTML candidate report outside the repository, then present ranked options. Do not implement candidates automatically. A CI pass is not proof of structural deepening.
- The upstream "grilling" skill is NOT installed in AutoWork at this baseline. Never claim to invoke it; use direct human decision questions and codebase-design's design-it-twice methodology, or request a separately approved dependency installation.
- The upstream HTML template uses CDN assets. Offline/no-network runs must produce a useful self-contained static fallback with local CSS/diagrams, and must not download Tailwind/Mermaid implicitly.
- No G2/G3 policy broadening, real CTI run, provider/Bridge egress, live PostgreSQL/MinIO access, deployment, migration, or S3 route changes arise from this skill installation.
- The new skill is disabled for implicit invocation. Confirm discovery in a fresh Paseo / Claude Code session after checkout; the file path alone does not prove runtime registration.

See docs/mission05-phase3/ARCHITECTURE_PREFLIGHT.md for the initial read-only examination at main 2f9c7454d1f980cb09543e93fc0e469c15eaa668.
