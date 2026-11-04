import { useEffect, useState } from "react";
import { builtinAvatarId, selectedDefaultAvatar } from "../avatar";

interface AvatarDisplayProps {
  value?: string | null;
  name: string;
  className?: string;
  label?: string;
}

export function AvatarDisplay({ value, name, className = "", label }: AvatarDisplayProps) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [value]);
  const accessibleLabel = label ?? `${name || "数字人"}的头像`;
  const builtinId = builtinAvatarId(value);
  if (builtinId) {
    const avatar = selectedDefaultAvatar(builtinId);
    return <img className={`digitalHumanAvatar ${className}`.trim()} src={avatar.source} alt={accessibleLabel}/>;
  }
  if (value && !failed) {
    return <img className={`digitalHumanAvatar ${className}`.trim()} src={value} alt={accessibleLabel} onError={() => setFailed(true)}/>;
  }
  return <span className={`avatarInitialFallback ${className}`.trim()} role="img" aria-label={accessibleLabel}>{name.trim().slice(0, 1) || "专"}</span>;
}
