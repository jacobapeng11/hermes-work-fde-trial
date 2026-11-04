import { afterEach, describe, expect, it, vi } from "vitest";

async function loadChatPerformanceFlags() {
  vi.resetModules();
  return (await import("./chatPerformanceFlags")).chatPerformanceFlags;
}

describe("chatPerformanceFlags", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("enables immediate first paint but keeps later deltas event-driven by default", async () => {
    const chatPerformanceFlags = await loadChatPerformanceFlags();

    expect(chatPerformanceFlags.firstVisibleImmediate).toBe(true);
    expect(chatPerformanceFlags.deltaFrameBatching).toBe(false);
  });

  it("enables frame batching only when explicitly requested", async () => {
    vi.stubEnv("VITE_TTFT_DELTA_FRAME_BATCHING", "true");

    expect((await loadChatPerformanceFlags()).deltaFrameBatching).toBe(true);

    vi.stubEnv("VITE_TTFT_DELTA_FRAME_BATCHING", "false");
    expect((await loadChatPerformanceFlags()).deltaFrameBatching).toBe(false);

    vi.stubEnv("VITE_TTFT_DELTA_FRAME_BATCHING", "unexpected");
    expect((await loadChatPerformanceFlags()).deltaFrameBatching).toBe(false);
  });
});
