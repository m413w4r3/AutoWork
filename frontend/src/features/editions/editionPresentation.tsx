import type { EditionStatus, Tlp } from "../../api/editions";

export const statusLabels: Record<EditionStatus, string> = {
  open: "Ouverte",
  archived: "Archivée",
};

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

export function formatPeriod(periodStart: string) {
  return new Intl.DateTimeFormat("fr-FR", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${periodStart}T00:00:00Z`));
}
