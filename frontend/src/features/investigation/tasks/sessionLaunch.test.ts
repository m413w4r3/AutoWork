import { describe, expect, it } from "vitest";

import type { AgentSessionCapabilitiesView } from "../api/agentSessions";
import { WorkshopApiError } from "../api/client";
import { makeTask } from "./testFixtures";
import {
  ADAPTER_EXECUTABLES,
  confinementAttested,
  confinementGap,
  describeSessionRefusal,
  openSessionLabel,
  operatorAdapterForTask,
  sessionLaunchTarget,
} from "./sessionLaunch";

function makeCapabilities(
  overrides: Partial<AgentSessionCapabilitiesView> = {},
): AgentSessionCapabilitiesView {
  return {
    adapter_kind: "codex_cli",
    executable: "codex",
    available: true,
    version: "codex-cli 0.163.0",
    capability_sha256: "a".repeat(64),
    probed_at: "2026-11-22T10:05:00Z",
    supports_resume: true,
    supports_structured_output: true,
    supports_non_interactive: true,
    modes: ["non_interactive"],
    permission_modes: [],
    confinement: {
      profile: "bwrap-loopback",
      bwrap_ok: true,
      netns: true,
      network_mode: "isolated_egress",
      loopback_isolated: true,
    },
    ...overrides,
  };
}

function assignedOperatorTask(overrides: Parameters<typeof makeTask>[0] = {}) {
  return makeTask({
    status: "assigned",
    assigned_actor_kind: "operator",
    assigned_actor_id: "operator:codex_cli",
    handoff_ref: "33333333-3333-4333-8333-333333333333",
    ...overrides,
  });
}

describe("operatorAdapterForTask", () => {
  it("parses the canonical operator actor ids only", () => {
    expect(operatorAdapterForTask(assignedOperatorTask())).toBe("codex_cli");
    expect(
      operatorAdapterForTask(
        assignedOperatorTask({ assigned_actor_id: "operator:claude_code" }),
      ),
    ).toBe("claude_code");
    expect(
      operatorAdapterForTask(
        assignedOperatorTask({
          assigned_actor_kind: "human",
          assigned_actor_id: "operator:codex_cli",
        }),
      ),
    ).toBeNull();
    expect(
      operatorAdapterForTask(
        assignedOperatorTask({ assigned_actor_id: "operator:unknown" }),
      ),
    ).toBeNull();
    expect(operatorAdapterForTask(makeTask())).toBeNull();
  });
});

describe("sessionLaunchTarget", () => {
  it("requires an assigned operator task with a handoff", () => {
    expect(sessionLaunchTarget(assignedOperatorTask())).toEqual({
      taskId: assignedOperatorTask().id,
      adapterKind: "codex_cli",
      handoffId: "33333333-3333-4333-8333-333333333333",
      capability: { adapterKind: "codex_cli", executable: "codex" },
    });
    expect(
      sessionLaunchTarget(assignedOperatorTask({ handoff_ref: null })),
    ).toBeNull();
    expect(
      sessionLaunchTarget(assignedOperatorTask({ status: "ready" })),
    ).toBeNull();
    expect(
      sessionLaunchTarget(
        assignedOperatorTask({ assigned_actor_kind: "human" }),
      ),
    ).toBeNull();
  });

  it("uses the PATH name the host resolves per adapter", () => {
    expect(ADAPTER_EXECUTABLES.codex_cli).toBe("codex");
    expect(ADAPTER_EXECUTABLES.claude_code).toBe("claude");
    expect(
      sessionLaunchTarget(
        assignedOperatorTask({ assigned_actor_id: "operator:claude_code" }),
      )?.capability.executable,
    ).toBe("claude");
  });
});

describe("confinement attestation", () => {
  it("accepts only the loopback-isolated egress attestation", () => {
    expect(confinementAttested(makeCapabilities())).toBe(true);
    expect(confinementAttested(makeCapabilities({ available: false }))).toBe(
      false,
    );
    expect(confinementAttested(makeCapabilities({ confinement: null }))).toBe(
      false,
    );
    expect(
      confinementAttested(
        makeCapabilities({
          confinement: {
            profile: "host",
            bwrap_ok: true,
            netns: false,
            network_mode: "host_network_unsafe",
            loopback_isolated: false,
          },
        }),
      ),
    ).toBe(false);
  });

  it("states the exact missing attestation", () => {
    const gap = confinementGap(
      makeCapabilities({
        confinement: {
          profile: "bwrap-loopback",
          bwrap_ok: true,
          netns: false,
          network_mode: "isolated_egress",
          loopback_isolated: false,
        },
      }),
    );
    expect(gap).toContain("loopback non isolé");
    expect(gap).toContain("netns non prouvé");
    expect(confinementGap(makeCapabilities({ confinement: null }))).toContain(
      "aucun confinement",
    );
  });
});

describe("describeSessionRefusal", () => {
  it("names the external-disclosure refusal", () => {
    expect(
      describeSessionRefusal(
        new WorkshopApiError(
          "refused",
          403,
          "agent_session_external_llm_not_allowed",
        ),
      ),
    ).toContain("Divulgation externe refusée");
    expect(
      describeSessionRefusal(
        new WorkshopApiError(
          "refused",
          403,
          "agent_session_external_llm_not_allowed",
          {
            reason: "gate_unavailable",
          },
        ),
      ),
    ).toContain("politique G2/TLP");
  });

  it("names confinement and host-agent refusals", () => {
    expect(
      describeSessionRefusal(
        new WorkshopApiError(
          "refused",
          503,
          "agent_session_network_isolation_unavailable",
          {
            reason: "loopback_not_isolated",
          },
        ),
      ),
    ).toContain("loopback-isolé");
    expect(
      describeSessionRefusal(
        new WorkshopApiError("refused", 503, "agentic_operator_unavailable"),
      ),
    ).toContain("Agent hôte absent");
    expect(
      describeSessionRefusal(
        new WorkshopApiError("refused", 503, "workspace_local_unavailable"),
      ),
    ).toContain("Agent hôte absent");
  });

  it("labels the open action per adapter", () => {
    expect(openSessionLabel("codex_cli")).toBe("Ouvrir la session Codex CLI");
    expect(openSessionLabel("claude_code")).toBe(
      "Ouvrir la session Claude Code",
    );
  });
});
