---
name: implement-spec
description: "Implement the result of /to-spec and /to-tickets in code."
disable-model-invocation: true
---
## AutoWork integration (scoped override)

Follow `AGENTS.md`, component rules and accepted ADRs first. Treat all model
output as untrusted; no CTI live calls, G2/G3 decisions, provider submissions,
malware execution, live DB/MinIO mutations, publication, unapproved issue
writes, force-push, remote merge or destructive cleanup.
Use Paseo to launch **new GPT-6 Luna high sessions** for bounded independent
coding work, **xhigh** for high-risk independent review. Claude-native agents
cannot be presumed Luna. Use isolated worktrees, effective access controls
(not just read-only words), exact base/result SHAs and focused offline oracles.
The parent must approve integration and external effects. Report missing
evidence as `UNKNOWN/BLOCKED`, not PASS.

Use the approved mission/tickets and authorize each write scope.
Delegation is independent fresh Luna high worktrees, optionally xhigh critical
review. TDD-style red/green through a focused offline oracle, independent
two-axis `code-review`, and a parent-controlled integration gate.
Never auto-merge, push, close tickets, publish or delete worktrees.


You have been provided a spec. This spec should have tickets associated with it, describing how to implement the spec.

Use the existing approved AutoWork task register/spec; no external setup skill is required.

The goal is verified per-ticket patches on isolated worktrees; the authorized parent controls integration and issue updates.

The tickets are not a list of steps. They are a **task graph** with blocking relationships between them. This means there is always a **frontier** of tickets which are ready to be grabbed.

Communication to and from subagents should be sparse. Communicate primarily through **context pointers**: to the spec, tickets, research notes, and previous commits. Don't duplicate information already available via pointers.

**Implementer subagents** should be run in the background where possible for maximum concurrency.

## Steps

1. Read the spec and tickets to understand the task graph.

2. (optional) Use an **exploration subagent** to conduct any exploration required by the tickets - relevant codebase files or external documentation. Ensure the exploration subagent can save files - it should save its markdown notes in a directory outside the repo, accessible by all future subagents. This lets **implementer subagents** focus on implementation rather than exploration.

3. Verify the approved integration base SHA and create an isolated worktree/branch as authorized. Draft PR creation and ticket updates require user approval.

4. Use **implementer subagents** to implement each ticket, each in its own worktree on its own branch. Each implementer subagent:
   - confirms its worktree is based on the integration branch before starting, and resets onto it if not;
   - uses a targeted red-capable test (or an already installed TDD skill) to implement the ticket;
   - reports its patch SHA and expected base; do not merge without parent approval

5. Parent reviews each patch/oracle and integrates only explicitly approved changes; no automatic merge.

6. If this changes the **frontier** of available tickets, kick off more **implementer subagents** to work on the new tickets. This allows for maximum concurrency.

7. Once all tickets are complete, call the Skill tool with `code-review` on the integration branch. Fix all issues raised by the code review in a single **implementer subagent**.

8. Keep draft PRs in draft until explicitly approved; report tickets and integration as proposed, not closed.

9. Remove only owned and confirmed-clean worktrees after authorization; keep incomplete changes.
