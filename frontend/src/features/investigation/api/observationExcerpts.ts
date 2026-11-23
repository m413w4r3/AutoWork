/** Bounded canonical observation reads used by the `canonical_code` picker. */

import { useQuery } from "@tanstack/react-query";

import { getJson, investigationPath, queryString } from "./client";
import type { Uuid } from "./types";

export type ObservationExcerptView = "text" | "hex";

interface ObservationExcerptMetadataV1 {
  offset: number;
  length: number;
  total_size: number;
  blob_sha256: string;
  excerpt_sha256: string;
  untrusted_data: true;
}

export interface ObservationExcerptTextV1 extends ObservationExcerptMetadataV1 {
  view: "text";
  text: string;
}

export interface ObservationExcerptHexV1 extends ObservationExcerptMetadataV1 {
  view: "hex";
  hex: string;
}

export type ObservationExcerptV1 =
  ObservationExcerptTextV1 | ObservationExcerptHexV1;

export interface ObservationExcerptParams {
  offset: number;
  length: number;
  view: ObservationExcerptView;
}

export const observationExcerptKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "observation-excerpts"] as const,
  detail: (
    investigationId: string,
    observationId: string,
    params: ObservationExcerptParams,
  ) =>
    [
      ...observationExcerptKeys.all(investigationId),
      observationId,
      params.offset,
      params.length,
      params.view,
    ] as const,
};

export function getObservationExcerpt(
  investigationId: string,
  observationId: Uuid,
  params: ObservationExcerptParams,
  signal?: AbortSignal,
): Promise<ObservationExcerptV1> {
  const suffix =
    `/observations/${encodeURIComponent(observationId)}/excerpt` +
    queryString({
      offset: params.offset,
      length: params.length,
      view: params.view,
    });
  return getJson<ObservationExcerptV1>(
    investigationPath(investigationId, suffix),
    signal,
  );
}

export function useObservationExcerpt(
  investigationId: string,
  observationId: Uuid | null,
  params: ObservationExcerptParams,
  enabled = true,
) {
  return useQuery({
    queryKey: observationExcerptKeys.detail(
      investigationId,
      observationId ?? "",
      params,
    ),
    queryFn: ({ signal }) => {
      if (observationId === null) {
        throw new Error("An observation is required to read an excerpt.");
      }
      return getObservationExcerpt(
        investigationId,
        observationId,
        params,
        signal,
      );
    },
    enabled: enabled && investigationId !== "" && observationId !== null,
  });
}
