import type { UserProfile } from "../profile";
import { avatarValueForProfile } from "../profile";
import { AvatarDisplay } from "./AvatarDisplay";

interface DigitalHumanAvatarProps {
  profile: Pick<UserProfile, "avatarKind" | "avatarId" | "customAvatarUrl" | "displayName">;
  className?: string;
  label?: string;
}

export function DigitalHumanAvatar({ profile, className = "", label }: DigitalHumanAvatarProps) {
  return <AvatarDisplay value={avatarValueForProfile(profile)} name={profile.displayName} className={className} label={label}/>;
}
