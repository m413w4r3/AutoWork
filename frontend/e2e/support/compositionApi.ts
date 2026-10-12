/*
 * Composition fixtures mirror the edition composition and validation wire
 * responses consumed by CompositionConsole. Keep mutations versioned so an
 * E2E only passes when the UI sends the current expected_version.
 */

export interface CompositionSubjectFixture {
  subject_id: string;
  title: string;
  production_run_id: string;
  pipeline_generation: number;
  artifact_id: string;
  artifact_version: number;
  artifact_input_hash: string;
  provenance_batch_id: string;
  document_sha256: string;
}

export function createCompositionApi(
  editionId: string,
  subjects: CompositionSubjectFixture[],
) {
  let version = 0;
  let entries: Array<Record<string, unknown>> = [];

  const read = () => ({
    edition_id: editionId,
    composition:
      version === 0
        ? null
        : {
            id: "composition-1",
            edition_id: editionId,
            version,
            actor_id: "analyst@example.test",
            preparation_status: "ready",
            created_at: "2026-08-29T00:10:00Z",
            updated_at: "2026-08-29T00:10:00Z",
            entries,
          },
    reasons:
      version === 0
        ? [
            {
              code: "composition_missing",
              message: "La composition n’est pas encore créée.",
              subject_id: null,
              details: null,
            },
          ]
        : [],
    axes: {
      production_validity: true,
      discovery_update_available: false,
      composition_eligibility: true,
    },
  });

  const validation = () => {
    const included = entries
      .filter((entry) => entry.disposition === "included")
      .slice()
      .sort(
        (left, right) =>
          Number(left.position ?? 0) - Number(right.position ?? 0),
      );
    return {
      edition_id: editionId,
      can_accept:
        included.length === subjects.length &&
        entries.length === subjects.length,
      composition_version: version || null,
      diagnostics: [],
      items: included.map((entry) => {
        const fixture = subjects.find(
          (subject) => subject.subject_id === entry.subject_id,
        );
        return {
          position: Number(entry.position),
          subject_id: String(entry.subject_id),
          title: fixture?.title ?? "Sujet",
          run_id: entry.production_run_id,
          pipeline_generation: entry.pipeline_generation,
          run_status: "ready",
          document_artifact_id: entry.artifact_id,
          document_artifact_version: entry.artifact_version,
          document_input_hash: entry.artifact_input_hash,
          effective_decision_id: null,
          effective_decision: "include",
          included: true,
          blocking: false,
          can_retry: false,
          retry_stage: null,
          requires_reconciliation: false,
          reconciliation: null,
          error_code: null,
          error_message: null,
          superseded_artifact_available: false,
          pinned_generation_current: true,
          latest_artifact_run_id: fixture?.production_run_id ?? null,
          latest_artifact_id: fixture?.artifact_id ?? null,
          latest_artifact_version: fixture?.artifact_version ?? null,
        };
      }),
    };
  };

  const put = (
    expectedVersion: number,
    nextEntries: Array<Record<string, unknown>>,
  ) => {
    if (expectedVersion !== version) {
      throw new Error(
        `Expected composition version ${expectedVersion}, current ${version}`,
      );
    }
    version += 1;
    entries = nextEntries.map((entry) => ({
      id: `entry-${String(entry.subject_id).slice(-4)}`,
      preparation_status: "ready",
      ...entry,
    }));
    return read().composition;
  };

  const accept = (
    expectedVersion: number,
    manifestId: string,
    sha256: string,
  ) => {
    if (expectedVersion !== version || !validation().can_accept) {
      throw new Error("Composition cannot be accepted at this version.");
    }
    return {
      edition_id: editionId,
      edition_state: "open",
      manifest_id: manifestId,
      manifest_sha256: sha256,
      manifest_schema_version: "2",
      edition_version: 4,
      composition_id: "composition-1",
      composition_version: expectedVersion,
      job_id: "assembly-job-1",
      job_dispatched: true,
    };
  };

  return {
    read,
    validation,
    put,
    accept,
    get version() {
      return version;
    },
  };
}
