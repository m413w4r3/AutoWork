---
name: autowork-context-handoff
description: Create a verifiable handoff to a fresh agent session when a task grows large, changes phase, or accumulates irrelevant context.
---

Use for context pressure, a completed lot with another lot pending, repeated
tool-output noise, or an upcoming specialist handoff. Do not wait for window
exhaustion. If usage is measured, treat ~50–60% occupied as a review cue,
not a hard cap; earlier handoff is preferable at a natural task boundary.

1. Verify current base/HEAD and dirty diff; never assert unsaved work exists
   merely because the conversation says so. Keep the task registry canonical.
2. Write a minimal **safe** handoff artifact in the approved coding worktree
   or report in the parent registry: TASK_ID; exact objective; base/output SHA;
   paths:lines; linked ADR/contract versions; changes done; decisive test
   results; blocked/unknown facts; next acceptance oracle; permissions.
3. Point to full logs and artifacts; do not copy transcripts, secrets,
   sample bytes, signed URLs or CTI evidence into the handoff.
4. Launch a genuinely **new** Paseo agent session, preferably Luna high
   (xhigh for complex reconciliation), only using verified runtime tools
   and current host profiles. Pass the compact handoff plus direct source
   pointers. If spawning is unavailable, finish with a handoff instead of
   claiming a fresh session exists.
5. New agent checks Git SHA, current AGENTS/ADR, scope and oracle before work;
   no previous model claim becomes a source of truth.

Acceptance: the fresh agent can resume from refs/SHAs and reproducible tests
without reading the old transcript. This is coding workflow state, never
canonical CTI evidence.
