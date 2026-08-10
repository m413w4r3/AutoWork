export type DependencyState = "ok" | "unavailable";

export interface DependencyHealth {
  status: DependencyState;
  detail: string | null;
}

export interface ReadyHealth {
  status: DependencyState;
  dependencies: Record<string, DependencyHealth>;
}

export async function fetchReadiness(): Promise<ReadyHealth> {
  const response = await fetch("/api/health/ready", {
    headers: { Accept: "application/json" },
  });

  if (!response.ok && response.status !== 503) {
    throw new Error(`Le backend a répondu avec le statut ${response.status}`);
  }

  return (await response.json()) as ReadyHealth;
}
