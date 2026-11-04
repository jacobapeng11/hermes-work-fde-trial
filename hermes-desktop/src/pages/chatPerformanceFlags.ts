export const chatPerformanceFlags = {
  firstVisibleImmediate: import.meta.env.VITE_TTFT_FIRST_VISIBLE_IMMEDIATE !== "false",
  deltaFrameBatching: import.meta.env.VITE_TTFT_DELTA_FRAME_BATCHING === "true"
} as const;
