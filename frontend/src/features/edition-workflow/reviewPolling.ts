import type { EditionReview } from "../../api/publication";

export function reviewPollingInterval(
  review: EditionReview | undefined,
): number | false {
  return review?.items.some(
    (item) => item.run_status === "queued" || item.run_status === "running",
  )
    ? 2_000
    : false;
}
