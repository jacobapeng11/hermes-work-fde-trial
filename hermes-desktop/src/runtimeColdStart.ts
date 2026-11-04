type ErrorWithStatus = Error & { status?: unknown };

const HTTP_503_PATTERN = /(?:^|[^0-9])503(?:[^0-9]|$)/;
const RUNTIME_NOT_READY_PATTERN = /runtime is not ready/i;

function hasHttpStatus(error: ErrorWithStatus, status: number): boolean {
  return error.status === status;
}

/**
 * Return whether a cold-start error should mark the runtime connection failed.
 *
 * A 503 response means Hermes is still converging; the UI can remain in its
 * starting state instead of asking the user to reload.
 */
export function isCriticalRuntimeError(error: unknown): boolean {
  if (!(error instanceof Error)) return true;
  if (hasHttpStatus(error, 503)) return false;
  return !(
    RUNTIME_NOT_READY_PATTERN.test(error.message)
    || HTTP_503_PATTERN.test(error.message)
  );
}
