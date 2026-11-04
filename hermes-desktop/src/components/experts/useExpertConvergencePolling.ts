import { useEffect, useMemo, useRef, useState } from "react";
import type { RuntimeAgentProfileSummary } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";

const POLL_DELAYS_MS = [1_500, 2_000, 3_000] as const;

const isConvergencePending = (profile: RuntimeAgentProfileSummary): boolean => {
  if (!["active", "provisioning", "updating"].includes(profile.status)) return false;
  if (["failed", "error", "disabled", "revoked"].includes(profile.convergenceStatus || "")) {
    return false;
  }
  return (
    (profile.desiredRevision ?? 0) > (profile.appliedRevision ?? 0)
    || ["waiting", "updating", "unconfigured"].includes(profile.convergenceStatus || "")
  );
};

export function useExpertConvergencePolling({
  runtimeWorkspaceId,
  profiles,
  pendingProfileIds = [],
  onProfileUpdated,
  onProjectionApplied
}: {
  runtimeWorkspaceId: number | null;
  profiles: RuntimeAgentProfileSummary[];
  pendingProfileIds?: readonly number[];
  onProfileUpdated: (profile: RuntimeAgentProfileSummary) => void;
  onProjectionApplied: (options?: { awaitSync?: boolean }) => Promise<void>;
}) {
  const [pollNonce, setPollNonce] = useState(0);
  const generationRef = useRef(0);
  const attemptRef = useRef(0);
  const pollKeyRef = useRef("");
  const updateProfileRef = useRef(onProfileUpdated);
  const projectionAppliedRef = useRef(onProjectionApplied);

  const pendingKey = useMemo(
    () => {
      const profileIds = new Set<number>([
        ...profiles
          .filter(isConvergencePending)
          .map((profile) => profile.runtimeAgentProfileId),
        ...pendingProfileIds,
      ]);
      return [...profileIds]
        .filter((profileId) => Number.isInteger(profileId) && profileId > 0)
        .sort((left, right) => left - right)
        .join(",");
    },
    [profiles, pendingProfileIds]
  );

  useEffect(() => {
    updateProfileRef.current = onProfileUpdated;
    projectionAppliedRef.current = onProjectionApplied;
  }, [onProfileUpdated, onProjectionApplied]);

  useEffect(() => {
    if (!runtimeWorkspaceId || !pendingKey) {
      pollKeyRef.current = "";
      attemptRef.current = 0;
      return;
    }

    const pollKey = `${runtimeWorkspaceId}:${pendingKey}`;
    if (pollKeyRef.current !== pollKey) {
      pollKeyRef.current = pollKey;
      attemptRef.current = 0;
    }

    const profileIds = pendingKey
      .split(",")
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value > 0);
    if (profileIds.length === 0) return;

    let cancelled = false;
    const generation = ++generationRef.current;
    const delay = POLL_DELAYS_MS[Math.min(
      attemptRef.current,
      POLL_DELAYS_MS.length - 1
    )];
    const timer = window.setTimeout(() => {
      void (async () => {
        let scheduleNext = false;
        try {
          const responses = await Promise.allSettled(
            profileIds.map((profileId) => userApi.expertDetail(
              runtimeWorkspaceId,
              profileId
            ))
          );
          if (cancelled || generation !== generationRef.current) return;

          attemptRef.current += 1;
          const nextProfiles = responses.flatMap((response) => (
            response.status === "fulfilled" ? [response.value] : []
          ));
          const stillPending = (
            nextProfiles.length === 0
            || nextProfiles.some(isConvergencePending)
          );
          const reachedApplied = nextProfiles.some((profile) => (
            !isConvergencePending(profile)
            && profile.convergenceStatus === "applied"
          ));

          nextProfiles.forEach((profile) => updateProfileRef.current(profile));
          if (reachedApplied) await projectionAppliedRef.current({ awaitSync: true });
          scheduleNext = stillPending;
        } finally {
          if (
            !cancelled
            && generation === generationRef.current
            && scheduleNext
          ) {
            setPollNonce((current) => current + 1);
          }
        }
      })();
    }, delay);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [runtimeWorkspaceId, pendingKey, pollNonce]);
}
