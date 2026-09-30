import type { EditionStatus, Tlp } from "../../api/editions";
import { statusLabels } from "./editionPresentationUtils";

export function StatusBadge({ state }: { state: EditionStatus }) {
  return (
    <span className={`badge badge--status badge--${state}`}>
      {statusLabels[state]}
    </span>
  );
}

export function TlpBadge({ tlp }: { tlp: Tlp }) {
  return (
    <span className={`badge badge--tlp-${tlp.toLowerCase().replace("+", "-")}`}>
      TLP:{tlp}
    </span>
  );
}
