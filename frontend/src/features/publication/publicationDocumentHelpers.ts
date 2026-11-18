export function publicationParagraphFingerprint(text: string): string {
  const folded = Array.from(text, (character) =>
    character === "ı" ? character : character.toUpperCase().toLowerCase(),
  )
    .join("")
    .replace(/ß/g, "ss");
  return folded.trim().split(/\s+/u).filter(Boolean).join(" ");
}

export function publicationTimelineDateLabel(entry: {
  event_date: string | null;
  date_text: string | null;
}): string {
  if (entry.date_text) return entry.date_text;
  if (entry.event_date) {
    return new Intl.DateTimeFormat("fr-FR", { dateStyle: "long" }).format(
      new Date(`${entry.event_date}T00:00:00`),
    );
  }
  return "";
}

export function publicationReferenceDateLabel(
  publishedAt: string | null,
): string {
  if (!publishedAt) return "Date de publication non précisée";
  const day = publishedAt.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) {
    return "Date de publication non précisée";
  }
  const parsed = new Date(`${day}T00:00:00`);
  return Number.isNaN(parsed.getTime())
    ? "Date de publication non précisée"
    : new Intl.DateTimeFormat("fr-FR", { dateStyle: "long" }).format(parsed);
}

export function publicationTimelineAnchor(index: number): string {
  return `timeline:${String(index + 1).padStart(4, "0")}`;
}

export function publicationLeadAnchor(index: number): string {
  return `lead:${String(index + 1).padStart(4, "0")}`;
}

export function publicationReferenceAnchor(index: number): string {
  return `reference:${String(index + 1).padStart(4, "0")}`;
}

export function publicationSectionParagraphAnchor(
  sectionIndex: number,
  paragraphIndex: number,
): string {
  return `section:${sectionIndex}:paragraph:${String(paragraphIndex + 1).padStart(4, "0")}`;
}
