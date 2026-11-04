import { useEffect, type ReactNode } from "react";
import {
  AuthRefreshError,
  getAccessTokenExpiresAt,
  isDevAuthMode,
  isPasswordAuthMode,
  redirectToLogin,
  refreshAccessToken
} from "../auth";

const REFRESH_AHEAD_MS = 2 * 60_000;
const PLATFORM_SESSION_KEEPALIVE_MS = 60_000;
const REFRESH_RETRY_MS = 30_000;

export function SessionLifecycle({ children }: { children: ReactNode }) {
  useEffect(() => {
    if (isDevAuthMode() || isPasswordAuthMode()) return;
    let stopped = false;
    let timer: number | undefined;

    const schedule = (fallbackMs = PLATFORM_SESSION_KEEPALIVE_MS) => {
      if (timer !== undefined) window.clearTimeout(timer);
      const expiresAt = getAccessTokenExpiresAt();
      const expiryDelayMs = expiresAt === null
        ? fallbackMs
        : Math.max(1_000, expiresAt - Date.now() - REFRESH_AHEAD_MS);
      const waitMs = Math.min(fallbackMs, expiryDelayMs);
      timer = window.setTimeout(async () => {
        try {
          await refreshAccessToken();
          if (!stopped) schedule();
        } catch (reason) {
          if (stopped) return;
          if (reason instanceof AuthRefreshError && reason.reauthRequired) {
            redirectToLogin();
            return;
          }
          schedule(REFRESH_RETRY_MS);
        }
      }, waitMs);
    };

    schedule();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  return <>{children}</>;
}
