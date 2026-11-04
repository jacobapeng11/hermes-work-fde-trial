import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  AuthRefreshError,
  getAccessTokenExpiresAt,
  isDevAuthMode,
  markReauthenticationManual,
  markAuthSessionActive,
  redirectToLogin,
  refreshAccessToken,
  requireReauthentication,
  reserveAutomaticReauthenticationAttempt,
  subscribeAuthSession,
  type AuthSessionState
} from "../auth";
import { userApi } from "../api/client";
import { ReauthenticationPrompt } from "./ReauthenticationPrompt";
import { usePlatformTheme } from "../theme/platform-theme";

const REFRESH_AHEAD_MS = 2 * 60_000;
const REFRESH_RETRY_MS = 30_000;
const PRESENCE_RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 30_000];
// Presence heartbeats arrive every 15s. When the page becomes visible again
// but the latest heartbeat is older than this threshold, the machine went
// through sleep/hibernate (a plain tab switch keeps heartbeats flowing), so a
// one-shot session probe is required before the user touches anything.
const WAKE_PROBE_THRESHOLD_MS = 45_000;
// After wake-up the network stack may still be initializing; retry once
// before declaring the network-flavored refresh failure a dead session.
const WAKE_PROBE_NETWORK_RETRY_MS = 10_000;

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

export function SessionLifecycle({ children }: { children: ReactNode }) {
  const [authState, setAuthState] = useState<AuthSessionState>("active");
  const [showReauthenticationPrompt, setShowReauthenticationPrompt] = useState(false);
  const presenceAbortRef = useRef<AbortController | null>(null);
  const reauthenticationStartedRef = useRef(false);
  const lastHeartbeatAtRef = useRef(0);
  // The dialog is a sibling of the themed application shell, so it must carry
  // the active theme itself instead of relying on CSS variable inheritance.
  const { theme } = usePlatformTheme();

  useEffect(() => subscribeAuthSession(setAuthState), []);

  useEffect(() => {
    if (authState !== "reauth_required") {
      reauthenticationStartedRef.current = false;
      setShowReauthenticationPrompt(false);
      return;
    }
    if (reauthenticationStartedRef.current) return;
    reauthenticationStartedRef.current = true;
    if (reserveAutomaticReauthenticationAttempt()) {
      redirectToLogin();
      return;
    }
    markReauthenticationManual();
    setShowReauthenticationPrompt(true);
  }, [authState]);

  // One-shot session probe for "the machine just woke up" moments. While the
  // session is alive the refresh silently renews it; any failure here means
  // the platform session died during sleep (the Enterprise SSO gateway can also
  // intercept the refresh into a non-JSON login page), so the page must enter
  // the reauthentication flow instead of retrying forever behind a stale UI.
  const probeSessionAfterWake = useCallback(async () => {
    if (isDevAuthMode()) return;
    if (Date.now() - lastHeartbeatAtRef.current < WAKE_PROBE_THRESHOLD_MS) return;
    try {
      await refreshAccessToken();
    } catch (reason) {
      if (reason instanceof AuthRefreshError && reason.status === 0) {
        await delay(WAKE_PROBE_NETWORK_RETRY_MS);
        try {
          await refreshAccessToken();
          return;
        } catch {
          // Fall through: still failing after the network settled.
        }
      }
      // 401 already triggered reauthentication inside refreshAccessToken;
      // re-publishing is idempotent and also converts the gateway-intercept
      // failure modes (CORS error, login-page HTML) into a login redirect.
      requireReauthentication();
    }
  }, []);

  // Browsers reliably fire visibilitychange when the tab is shown again after
  // the system resumes from sleep.
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState !== "visible") return;
      void probeSessionAfterWake();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [probeSessionAfterWake]);

  // Electron windows do not change visibility across a system sleep, so the
  // desktop shell forwards powerMonitor events over the preload bridge
  // instead. On the plain web this bridge is absent and the listener is a
  // no-op, keeping both copies of this file identical.
  useEffect(() => {
    const desktop = (window as Window & {
      coworkDesktop?: { onSystemResume?: (listener: () => void) => () => void };
    }).coworkDesktop;
    if (!desktop?.onSystemResume) return;
    return desktop.onSystemResume(() => {
      void probeSessionAfterWake();
    });
  }, [probeSessionAfterWake]);

  useEffect(() => {
    if (isDevAuthMode()) return;
    let stopped = false;
    let timer: number | undefined;

    const schedule = (fallbackMs = 60_000) => {
      if (timer !== undefined) window.clearTimeout(timer);
      const expiresAt = getAccessTokenExpiresAt();
      const waitMs = expiresAt === null
        ? fallbackMs
        : Math.max(1_000, expiresAt - Date.now() - REFRESH_AHEAD_MS);
      timer = window.setTimeout(async () => {
        try {
          await refreshAccessToken();
          if (!stopped) schedule();
        } catch (reason) {
          if (
            !stopped
            && (!(reason instanceof AuthRefreshError) || !reason.reauthRequired)
          ) {
            schedule(REFRESH_RETRY_MS);
          }
        }
      }, waitMs);
    };

    const unsubscribe = subscribeAuthSession((state) => {
      if (state === "active" && !stopped) schedule();
    });
    schedule();
    return () => {
      stopped = true;
      unsubscribe();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    let reconnectAttempt = 0;

    const run = async () => {
      while (!stopped) {
        const controller = new AbortController();
        presenceAbortRef.current = controller;
        try {
          await userApi.streamBrowserPresence((event) => {
            if (event.event === "presence.session_expired") {
              // The server confirmed the platform session is gone; do not
              // wait for the reconnect loop to discover the same 401.
              requireReauthentication();
              return;
            }
            if (
              event.event === "presence.heartbeat"
              && event.data.session === "active"
            ) {
              reconnectAttempt = 0;
              lastHeartbeatAtRef.current = Date.now();
              markAuthSessionActive();
            }
          }, controller.signal);
        } catch (reason) {
          if (stopped || controller.signal.aborted) return;
          if (reason instanceof AuthRefreshError && reason.reauthRequired) return;
        } finally {
          if (presenceAbortRef.current === controller) {
            presenceAbortRef.current = null;
          }
        }
        if (stopped) return;
        const reconnectDelay = PRESENCE_RECONNECT_DELAYS_MS[
          Math.min(reconnectAttempt, PRESENCE_RECONNECT_DELAYS_MS.length - 1)
        ];
        reconnectAttempt += 1;
        await delay(reconnectDelay);
      }
    };

    void run();
    return () => {
      stopped = true;
      presenceAbortRef.current?.abort();
      presenceAbortRef.current = null;
    };
  }, []);

  return <>
    {children}
    {authState === "service_unavailable" && (
      <div className="authReconnectNotice" role="status">
        连接正在恢复，当前页面内容会保留
      </div>
    )}
    {showReauthenticationPrompt && (
      <ReauthenticationPrompt
        theme={theme}
        onAuthenticate={() => redirectToLogin()}
      />
    )}
  </>;
}
