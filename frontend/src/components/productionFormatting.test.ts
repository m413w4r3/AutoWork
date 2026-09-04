import { describe, expect, it } from "vitest";

import { formatProductionWarning } from "./productionFormatting";

describe("formatProductionWarning source evidence rejections", () => {
  it("parse le warning simple avec son type, son compte et sa raison", () => {
    const warning = formatProductionWarning(
      "q2_source_evidence_rejected:S3:domain:count=4:reason=source_evidence_missing",
    );

    expect(warning.title).toBe("Indicateurs écartés");
    expect(warning.source).toBe("S3");
    expect(warning.message).toContain("4 valeur(s) de type domain");
    expect(warning.message).toContain("source_evidence_missing");
  });

  it("parse le warning de lot avec son type, son compte et sa raison", () => {
    const warning = formatProductionWarning(
      "q2_batch_source_evidence_rejected:B1:S3:filename:count=5:reason=source_evidence_missing",
    );

    expect(warning.title).toBe("Indicateurs écartés");
    expect(warning.source).toBe("S3");
    expect(warning.message).toContain("5 valeur(s) de type filename");
    expect(warning.message).toContain("source_evidence_missing");
  });
});
