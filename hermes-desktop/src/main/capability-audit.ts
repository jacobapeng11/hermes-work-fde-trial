import { appendFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";

type AuditEntry = {
  at: string;
  capability: "workspace" | "browser" | "command" | "platform";
  operation: string;
  target?: string;
  outcome: "started" | "completed" | "canceled" | "failed" | "pending_approval" | "denied" | "expired";
  detail?: string;
};

export async function appendCapabilityAudit(userDataPath: string, entry: Omit<AuditEntry, "at">): Promise<void> {
  const file = join(userDataPath, "logs", "capabilities.jsonl");
  await mkdir(dirname(file), { recursive: true });
  await appendFile(file, `${JSON.stringify({ at: new Date().toISOString(), ...entry })}\n`, "utf8");
}
