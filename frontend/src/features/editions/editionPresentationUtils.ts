import type { EditionStatus } from "../../api/editions";

export const statusLabels: Record<EditionStatus, string> = {
  open: "Ouverte",
  archived: "Archivée",
};

export function formatPeriod(periodStart: string) {
  return new Intl.DateTimeFormat("fr-FR", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${periodStart}T00:00:00Z`));
}
