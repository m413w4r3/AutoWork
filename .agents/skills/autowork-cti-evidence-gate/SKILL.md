---
name: autowork-cti-evidence-gate
description: Independently check an AutoWork CTI claim, sample association, detection rule or finding against canonical observations and G2/G3 gates.
---

Inputs: claim/finding/candidate/rule ID, version and SHA, exact observation
refs, authorized snapshots and applicable policy. Only read authoritative
evidence through existing interfaces; output a review, not a verdict mutation.

Check causal claim->test->observation grounding, counterexamples and coverage.
Do not infer active C2 from a string or DNS lookup. For YARA, distinguish
compile success, train positives, goodware false positives and holdout;
do not call a rule validated on training alone. Preserve absent/unobserved
versus refuted, and candidate versus confirmed membership.

Return a short matrix with refs/SHA, supported/contradicted/inconclusive
per claim, omissions and the next discriminating authorized test. Never
approve G2/G3, acquire malware, execute samples or publish.
