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
  if (
    release &&
    release.assembly_status !== null &&
    !isActiveAssembly(release.assembly_status)
  ) {
    return false;
  }
  return 2_000;
}
