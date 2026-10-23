---
name: autowork-ralph-gate
description: Apply a bounded test-driven repair loop to a reproducible offline coding failure, or progress across independently gated coding lots.
---

Only use with a deterministic, runnable offline oracle and a permitted
coding worktree. Not for open-ended "keep trying until happy" prompts,
external model calls, VirusTotal acquisition, ambiguous `needs_review`,
destructive migration, G3 or release.

Repair loop: record failing oracle + base SHA -> hypothesize cause ->
minimal patch -> focused check -> inspect diff and regression risk.
Stop at PASS, at 3 attempts per oracle, or after 2 attempts without
measured improvement (request independent Luna xhigh reviewer).
Do not re-run full integration suites at each attempt.

Long mission: take the next unblocked TASK_ID from ONE durable registry,
spawn a fresh Luna high session for that bounded lot, run its gate, record
`DONE/FAIL/BLOCKED/UNKNOWN` + output SHA, then start another fresh session
only for the next *independent* lot. The session's own declaration of
completion is not the oracle.

If no deterministic oracle exists, define an explicit human review gate,
not a self-graded Ralph success. Track attempts, tests, time and known costs;
unknown provider usage stays UNKNOWN.
