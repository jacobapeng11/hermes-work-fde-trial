import type { AvatarAssetSummary } from "@hermes-cloud/schemas";
import avatar01 from "./assets/digital-human-avatars/individual/avatar-01.png";
import avatar02 from "./assets/digital-human-avatars/individual/avatar-02.png";
import avatar03 from "./assets/digital-human-avatars/individual/avatar-03.png";
import avatar04 from "./assets/digital-human-avatars/individual/avatar-04.png";
import avatar05 from "./assets/digital-human-avatars/individual/avatar-05.png";
import avatar06 from "./assets/digital-human-avatars/individual/avatar-06.png";
import avatar07 from "./assets/digital-human-avatars/individual/avatar-07.png";
import avatar08 from "./assets/digital-human-avatars/individual/avatar-08.png";

export const BUILTIN_AVATAR_PREFIX = "builtin:";
export const DEFAULT_AVATAR_ID = "avatar-01";
const AVATAR_OUTPUT_SIZE = 256;
const ACCEPTED_AVATAR_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

export type DefaultAvatar = {
  id: string;
  label: string;
  source: string;
};

export const DEFAULT_AVATARS: readonly DefaultAvatar[] = [
  { id: "avatar-01", label: "默认头像 1", source: avatar01 },
  { id: "avatar-02", label: "默认头像 2", source: avatar02 },
  { id: "avatar-03", label: "默认头像 3", source: avatar03 },
  { id: "avatar-04", label: "默认头像 4", source: avatar04 },
  { id: "avatar-05", label: "默认头像 5", source: avatar05 },
  { id: "avatar-06", label: "默认头像 6", source: avatar06 },
  { id: "avatar-07", label: "默认头像 7", source: avatar07 },
  { id: "avatar-08", label: "默认头像 8", source: avatar08 }
];

export const DEFAULT_AVATAR_VALUE = `${BUILTIN_AVATAR_PREFIX}${DEFAULT_AVATAR_ID}`;

export function builtinAvatarId(value?: string | null): string | null {
  if (!value) return null;
  const candidate = value.startsWith(BUILTIN_AVATAR_PREFIX)
    ? value.slice(BUILTIN_AVATAR_PREFIX.length)
    : value;
  return DEFAULT_AVATARS.some((avatar) => avatar.id === candidate) ? candidate : null;
}

export function selectedDefaultAvatar(value?: string | null): DefaultAvatar {
  const avatarId = builtinAvatarId(value) || DEFAULT_AVATAR_ID;
  return DEFAULT_AVATARS.find((avatar) => avatar.id === avatarId) ?? DEFAULT_AVATARS[0];
}

export function builtinAvatarOptions(): AvatarAssetSummary[] {
  return DEFAULT_AVATARS.map((avatar, index) => ({
    avatarValue: `${BUILTIN_AVATAR_PREFIX}${avatar.id}`,
    label: avatar.label,
    sourceKind: "builtin",
    imageUrl: null,
    sortOrder: index + 1
  }));
}

export function validateAvatarFile(file: File): void {
  if (!ACCEPTED_AVATAR_TYPES.has(file.type)) throw new Error("请选择 PNG、JPEG 或 WebP 图片。");
}

export type AvatarCrop = {
  source: string;
  imageWidth: number;
  imageHeight: number;
  viewportSize: number;
  scale: number;
  offsetX: number;
  offsetY: number;
};

export async function renderAvatarCrop(file: File, crop: AvatarCrop): Promise<File> {
  const image = await loadImage(crop.source);
  const canvas = document.createElement("canvas");
  canvas.width = AVATAR_OUTPUT_SIZE;
  canvas.height = AVATAR_OUTPUT_SIZE;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("浏览器不支持头像处理，请换用其他浏览器。");
  const factor = AVATAR_OUTPUT_SIZE / crop.viewportSize;
  const renderedWidth = crop.imageWidth * crop.scale;
  const renderedHeight = crop.imageHeight * crop.scale;
  context.fillStyle = "#f2f4f3";
  context.fillRect(0, 0, AVATAR_OUTPUT_SIZE, AVATAR_OUTPUT_SIZE);
  context.drawImage(
    image,
    ((crop.viewportSize - renderedWidth) / 2 + crop.offsetX) * factor,
    ((crop.viewportSize - renderedHeight) / 2 + crop.offsetY) * factor,
    renderedWidth * factor,
    renderedHeight * factor
  );
  const blob = await canvasBlob(canvas);
  return new File([blob], normalizedFilename(file.name), { type: "image/jpeg" });
}

export async function normalizeAvatarFile(file: File): Promise<File> {
  validateAvatarFile(file);
  return file;
}

export function avatarFileFromDataUrl(dataUrl: string): File {
  const match = /^data:(image\/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)$/.exec(dataUrl);
  if (!match) throw new Error("旧头像数据无法读取，请重新上传。");
  const bytes = Uint8Array.from(atob(match[2]), (character) => character.charCodeAt(0));
  const extension = match[1] === "image/jpeg" ? "jpg" : match[1].slice("image/".length);
  return new File([bytes], `avatar-migrated.${extension}`, { type: match[1] });
}

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new Error("头像处理失败，请换一张后重试。")),
      "image/jpeg",
      0.86
    );
  });
}

function normalizedFilename(filename: string): string {
  const stem = filename.replace(/\.[^.]+$/, "");
  return `${stem || "avatar"}.jpg`;
}

function loadImage(source: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onerror = () => reject(new Error("图片无法读取，请换一张后重试。"));
    image.onload = () => resolve(image);
    image.src = source;
  });
}
