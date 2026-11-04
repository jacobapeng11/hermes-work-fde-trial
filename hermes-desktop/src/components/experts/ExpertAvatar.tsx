import { AvatarDisplay } from "../AvatarDisplay";

export function ExpertAvatar({
  name,
  avatarUrl,
  size = "normal",
}: {
  name: string;
  avatarUrl?: string | null;
  size?: "normal" | "small";
}) {
  return <span className="expertAvatar" data-size={size}>
    <AvatarDisplay value={avatarUrl} name={name} label={`${name}的头像`}/>
  </span>;
}
