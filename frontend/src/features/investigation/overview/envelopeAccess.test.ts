import { describe, expect, it } from "vitest";

import { envelopeAccessState } from "./envelopeAccess";

const baseEnvelope = {
  approved: true,
  currently_authorized: true,
  allowed_providers: ["vt", "bridge"],
  acquire_hits_allowed: true,
  max_acquire_volume: 2,
  disclosure: {
    tlp: "amber" as const,
    do_not_submit: false,
    external_llm_allowed: true,
  },
  authority_kind: "human_g2",
  policy_status: null,
};

const cases = [
  {
    name: "never approved",
    overrides: {
      approved: false,
      currently_authorized: false,
      allowed_providers: [] as string[],
      acquire_hits_allowed: false,
      authority_kind: null,
    },
    expected: "never_approved",
  },
  {
    name: "approved and active",
    overrides: {},
    expected: "active",
  },
  {
    name: "extended and active",
    overrides: { version: 2 },
    expected: "active",
  },
  {
    name: "explicitly revoked",
    overrides: {
      currently_authorized: false,
      authority_kind: "human_g2",
    },
    expected: "revoked",
  },
  {
    name: "policy superseded",
    overrides: {
      currently_authorized: false,
      authority_kind: "osint_policy",
      policy_status: "superseded",
    },
    expected: "inactive",
  },
  {
    name: "other non-authorized policy state",
    overrides: {
      currently_authorized: false,
      authority_kind: "osint_policy",
      policy_status: "active",
    },
    expected: "inactive",
  },
] as const;

describe("envelopeAccessState", () => {
  it.each(cases)(
    "classifies $name from the envelope response",
    ({ overrides, expected }) => {
      const access = envelopeAccessState({ ...baseEnvelope, ...overrides });
      expect(access.kind).toBe(expected);
    },
  );

  it.each(cases.filter(({ expected }) => expected !== "active"))(
    "does not label recorded rights as authorized for $name",
    ({ overrides }) => {
      const access = envelopeAccessState({ ...baseEnvelope, ...overrides });
      const rightLabels = [
        access.providersLabel,
        access.acquisitionLabel,
        access.journalAcquisitionLabel,
        access.inactiveDisclosureLabel,
        access.providerPermissionLabel("vt", "VirusTotal (lectures)", true),
        access.providerPermissionLabel("vt", "VirusTotal (lectures)", false),
      ];

      expect(access.isActive).toBe(false);
      expect(access.providersLabel).toContain("Fournisseurs enregistrés");
      if (access.kind !== "never_approved") {
        expect(access.providersLabel).toContain("(non actifs)");
      }
      expect(rightLabels.join(" ")).not.toMatch(
        /\b(?:autorisé|autorisée|autorisés|autorisées)\b/i,
      );
      if (access.kind === "never_approved") {
        expect(access.acquisitionLabel).toContain("aucune approbation G2");
      } else {
        expect(access.acquisitionLabel).toMatch(/non active|inactifs/);
        expect(
          access.providerPermissionLabel("vt", "VirusTotal (lectures)", true),
        ).toContain("non actives");
        expect(
          access.providerPermissionLabel("vt", "VirusTotal (lectures)", false),
        ).toContain("accès inactifs");
      }
    },
  );

  it("keeps active acquisition and disclosure labels unchanged", () => {
    const access = envelopeAccessState(baseEnvelope);
    expect(access.acquisitionLabel).toBe("autorisée");
    expect(access.providersLabel).toBe("Fournisseurs autorisés");
    expect(access.statusLabel).toBe("Accès externes : actifs");
  });

  it("keeps explicit revocation distinct from other inactive states", () => {
    const access = envelopeAccessState({
      ...baseEnvelope,
      currently_authorized: false,
      authority_kind: "human_g2",
    });
    expect(access.statusLabel).toBe("Accès externes : révoqués");
    expect(access.statusMessage).toBe(
      "Aucune nouvelle opération VT, Shodan ou Bridge externe n’est autorisée. Les analyses locales restent accessibles.",
    );
  });
});
