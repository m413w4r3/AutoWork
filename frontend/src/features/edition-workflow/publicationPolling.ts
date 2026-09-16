import type {
  AssemblyJobStatus,
  EditionReleaseResponse,
} from "../../api/publication";

function isActiveAssembly(status: AssemblyJobStatus | null): boolean {
  return status === "queued" || status === "running";
}

export function publicationPollingInterval(
  release: EditionReleaseResponse | undefined,
): number | false {
  if (!release?.manifest_id || release.release_id) return false;
  if (release.can_retry_assembly) return false;
  return isActiveAssembly(release.assembly_status) ? 2_000 : false;
}
