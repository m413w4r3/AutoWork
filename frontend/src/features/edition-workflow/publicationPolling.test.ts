import { describe, expect, it } from "vitest";

import type {
  AssemblyJobStatus,
  EditionReleaseResponse,
  EditionRenderDisplayStatus,
} from "../../api/publication";
import { publicationPollingInterval } from "./publicationPolling";

function release(
  overrides: Partial<EditionReleaseResponse>,
): EditionReleaseResponse {
  return {
    edition_id: "edition-1",
    edition_state: "open",
    manifest_id: "manifest-1",
    manifest_sha256: "a".repeat(64),
    release_id: "release-1",
    json_available: true,
    render_id: null,
    render_status: "none",
    render_error_code: null,
    render_error_message: null,
    can_retry_render: false,
    pdf_available: false,
    published_at: null,
    assembly_job_id: null,
    assembly_status: null,
    assembly_error_code: null,
    assembly_error_message: null,
    can_retry_assembly: false,
    ...overrides,
  };
}

describe("publicationPollingInterval", () => {
  it("ne poll pas sans manifest figé", () => {
    expect(publicationPollingInterval(undefined)).toBe(false);
    expect(publicationPollingInterval(release({ manifest_id: null }))).toBe(
      false,
    );
  });

  it.each<[EditionRenderDisplayStatus, number | false]>([
    ["queued", 2_000],
    ["running", 2_000],
    ["failed", false],
    ["not_started", false],
    ["succeeded", false],
  ])("poll le rendu %s toutes les %s ms", (render_status, expected) => {
    expect(publicationPollingInterval(release({ render_status }))).toBe(
      expected,
    );
  });

  it.each<[AssemblyJobStatus | null, boolean, number | false]>([
    ["queued", false, 2_000],
    ["running", false, 2_000],
    ["failed", true, false],
    ["failed", false, false],
    [null, false, false],
  ])(
    "poll l’assemblage %s (relance possible : %s)",
    (assembly_status, can_retry_assembly, expected) => {
      expect(
        publicationPollingInterval(
          release({
            release_id: null,
            assembly_status,
            can_retry_assembly,
          }),
        ),
      ).toBe(expected);
    },
  );
});
