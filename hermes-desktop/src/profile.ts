import type { AuthPrincipalSummary } from "@hermes-cloud/schemas";
import {
  builtinAvatarId,
  BUILTIN_AVATAR_PREFIX,
  DEFAULT_AVATAR_ID,
  DEFAULT_AVATAR_VALUE,
  DEFAULT_AVATARS,
  normalizeAvatarFile
} from "./avatar";

const PROFILE_STORAGE_PREFIX = "hermes.user-profile.v1";

export type UserProfile = {
  displayName: string;
  jobTitle: string;
  team: string;
  signature: string;
  avatarKind: "default" | "custom";
  avatarId: string;
  customAvatarUrl: string | null;
};

export { DEFAULT_AVATARS, normalizeAvatarFile };

export function profileStorageKey(principal: Pick<AuthPrincipalSummary, "orgId" | "userId">): string {
  return `${PROFILE_STORAGE_PREFIX}.${principal.orgId}.${principal.userId}`;
}

export function defaultUserProfile(displayName?: string | null): UserProfile {
  return {
    displayName: normalizedValue(displayName, 24) || "我的数字人",
    jobTitle: "",
    team: "",
    signature: "",
    avatarKind: "default",
    avatarId: DEFAULT_AVATAR_ID,
    customAvatarUrl: null
  };
}

export function loadUserProfile(principal: AuthPrincipalSummary | null): UserProfile {
  const fallback = defaultUserProfile(principal?.name);
  if (!principal) return fallback;
  try {
    return parseStoredProfile(window.localStorage.getItem(profileStorageKey(principal)), fallback);
  } catch {
    return fallback;
  }
}

export function saveUserProfile(principal: AuthPrincipalSummary | null, profile: UserProfile): void {
  if (!principal) return;
  window.localStorage.setItem(profileStorageKey(principal), JSON.stringify(sanitizeProfile(profile, defaultUserProfile(principal.name))));
}

export function avatarValueForProfile(profile: Pick<UserProfile, "avatarKind" | "avatarId" | "customAvatarUrl">): string {
  if (profile.avatarKind === "custom" && profile.customAvatarUrl) return profile.customAvatarUrl;
  return `${BUILTIN_AVATAR_PREFIX}${builtinAvatarId(profile.avatarId) || DEFAULT_AVATAR_ID}`;
}

function parseStoredProfile(raw: string | null, fallback: UserProfile): UserProfile {
  if (!raw) return fallback;
  try {
    return sanitizeProfile(JSON.parse(raw), fallback);
  } catch {
    return fallback;
  }
}

function sanitizeProfile(value: unknown, fallback: UserProfile): UserProfile {
  const candidate = typeof value === "object" && value !== null ? value as Partial<UserProfile> : {};
  const legacyCandidate = candidate as Partial<UserProfile> & { customAvatarDataUrl?: unknown };
  const rawCustomAvatar = typeof candidate.customAvatarUrl === "string"
    ? candidate.customAvatarUrl
    : typeof legacyCandidate.customAvatarDataUrl === "string"
      ? legacyCandidate.customAvatarDataUrl
      : null;
  const customAvatarUrl = rawCustomAvatar && (/^https?:\/\//.test(rawCustomAvatar) || rawCustomAvatar.startsWith("data:image/"))
    ? rawCustomAvatar
    : null;
  const avatarKind = candidate.avatarKind === "custom" && customAvatarUrl ? "custom" : "default";
  const avatarId = DEFAULT_AVATARS.some((avatar) => avatar.id === candidate.avatarId) ? candidate.avatarId as string : DEFAULT_AVATAR_ID;
  return {
    displayName: normalizedValue(candidate.displayName, 24) || fallback.displayName,
    jobTitle: normalizedValue(candidate.jobTitle, 32),
    team: normalizedValue(candidate.team, 32),
    signature: normalizedValue(candidate.signature, 80),
    avatarKind,
    avatarId,
    customAvatarUrl: avatarKind === "custom" ? customAvatarUrl : null
  };
}

function normalizedValue(value: unknown, limit: number): string {
  return typeof value === "string" ? value.trim().slice(0, limit) : "";
}
