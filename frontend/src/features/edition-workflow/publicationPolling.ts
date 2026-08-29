import type { EditionStatus } from "../../api/editions";
import type {
  AssemblyJobStatus,
  EditionReleaseResponse,
} from "../../api/publication";

function isActiveAssembly(status: AssemblyJobStatus | null): boolean {
  return status === "queued" || status === "running";
}

export function publicationPollingInterval(
  editionStatus: EditionStatus,
  release: EditionReleaseResponse | undefined,
): number | false {
  if (editionStatus !== "assembling") return false;
  if (release?.edition_status === "published") return false;
  if (!release) return 2_000;
  if (release.can_retry_assembly) return false;
  return isActiveAssembly(release.assembly_status) ? 2_000 : false;
}
