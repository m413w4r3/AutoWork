import { createHash } from "node:crypto";
import { resolve } from "node:path";

const PORT_RANGE_START = 40_000;
const PORT_RANGE_SIZE = 20_000;

export function editorialPortForWorktree(
  worktreePath: string,
  requestedPort: string | null | undefined = process.env.EDITORIAL_E2E_PORT,
): number {
  if (requestedPort !== undefined && requestedPort !== null) {
    const port = Number(requestedPort);
    if (
      !/^\d+$/.test(requestedPort) ||
      !Number.isInteger(port) ||
      port < 1024 ||
      port > 65_535
    ) {
      throw new Error(
        "EDITORIAL_E2E_PORT must be an integer between 1024 and 65535",
      );
    }
    return port;
  }

  const digest = createHash("sha256").update(resolve(worktreePath)).digest();
  return PORT_RANGE_START + (digest.readUInt32BE(0) % PORT_RANGE_SIZE);
}

export function editorialAppOrigin(
  worktreePath = process.cwd(),
  requestedPort = process.env.EDITORIAL_E2E_PORT,
): string {
  return `http://127.0.0.1:${editorialPortForWorktree(worktreePath, requestedPort)}`;
}
