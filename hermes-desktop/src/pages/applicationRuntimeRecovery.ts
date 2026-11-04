import type {
  ApplicationAgentConfig,
  ApplicationAgentDetail
} from "@hermes-cloud/schemas";
import { ApiError } from "../api/client";

const APPLICATION_RUNTIME_BUSY = "application_agent_runtime_busy";

interface RetryOptions {
  delayMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
}

interface WaitForConfigApplyOptions extends RetryOptions {
  reason: unknown;
  expectedGeneration: number;
  targetConfig: ApplicationAgentConfig;
  loadAgent: () => Promise<ApplicationAgentDetail>;
}

function abortError(): DOMException {
  return new DOMException("The operation was aborted", "AbortError");
}

function wait(delayMs: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(abortError());
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function sameConfig(
  left: ApplicationAgentConfig,
  right: ApplicationAgentConfig
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function isApplicationRuntimeBusyError(reason: unknown): boolean {
  return reason instanceof ApiError
    && reason.status === 409
    && reason.code === APPLICATION_RUNTIME_BUSY;
}

export async function retryApplicationRuntimeBusy<T>(
  operation: () => Promise<T>,
  options: RetryOptions = {}
): Promise<T> {
  const delayMs = options.delayMs ?? 1_000;
  const timeoutMs = options.timeoutMs ?? 120_000;
  const startedAt = Date.now();
  let attempts = 0;
  const maxAttempts = Math.max(
    1,
    Math.ceil(timeoutMs / Math.max(delayMs, 1)) + 1
  );
  while (true) {
    if (options.signal?.aborted) throw abortError();
    try {
      return await operation();
    } catch (reason) {
      attempts += 1;
      if (
        !isApplicationRuntimeBusyError(reason)
        || attempts >= maxAttempts
        || Date.now() - startedAt >= timeoutMs
      ) throw reason;
      await wait(delayMs, options.signal);
    }
  }
}

export async function waitForApplicationConfigApplyAfterBusy({
  reason,
  expectedGeneration,
  targetConfig,
  loadAgent,
  delayMs = 1_000,
  timeoutMs = 120_000,
  signal
}: WaitForConfigApplyOptions): Promise<ApplicationAgentDetail> {
  if (!isApplicationRuntimeBusyError(reason)) throw reason;
  const startedAt = Date.now();
  const maxAttempts = Math.max(
    1,
    Math.ceil(timeoutMs / Math.max(delayMs, 1)) + 1
  );
  for (let attempts = 0; attempts < maxAttempts; attempts += 1) {
    if (signal?.aborted) throw abortError();
    const current = await loadAgent();
    if (
      current.configGeneration !== expectedGeneration
      || !sameConfig(current.config, targetConfig)
    ) throw reason;
    if (current.applyStatus === "failed") {
      throw new Error(
        current.lastApplyErrorCode
        || "应用 Agent Runtime 配置应用失败"
      );
    }
    if (
      current.applyStatus === "applied"
      && current.appliedGeneration === expectedGeneration
    ) return current;
    if (
      attempts + 1 >= maxAttempts
      || Date.now() - startedAt >= timeoutMs
    ) break;
    await wait(delayMs, signal);
  }
  throw new Error("应用 Agent Runtime 重新预热超时，请稍后刷新");
}
