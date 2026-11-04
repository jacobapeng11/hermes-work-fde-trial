export const DEFAULT_TASK_TITLE = "新任务";
const LEGACY_DEFAULT_TASK_TITLE = "新会话";
const FIRST_MESSAGE_TITLE_LENGTH = 10;

export function isDefaultTaskTitle(title?: string | null): boolean {
  const normalized = title?.trim();
  return !normalized || normalized === DEFAULT_TASK_TITLE || normalized === LEGACY_DEFAULT_TASK_TITLE;
}

export function firstMessageTaskTitle(message: string): string {
  const normalized = message.trim().replace(/\s+/g, " ");
  const characters = Array.from(normalized);
  const title = characters.slice(0, FIRST_MESSAGE_TITLE_LENGTH).join("");
  return characters.length > FIRST_MESSAGE_TITLE_LENGTH ? `${title}…` : title;
}

export function numberedTaskTitle(baseTitle: string, candidateNumber: number): string {
  return candidateNumber <= 1 ? baseTitle : `${baseTitle}（${candidateNumber}）`;
}

export function displayTaskTitle(title?: string | null): string {
  if (isDefaultTaskTitle(title)) return DEFAULT_TASK_TITLE;
  return title as string;
}
