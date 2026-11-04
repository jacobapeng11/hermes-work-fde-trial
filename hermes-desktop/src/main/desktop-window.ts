export interface DesktopWorkArea {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface DesktopWindowBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

const DEFAULT_WIDTH = 1440;
const DEFAULT_HEIGHT = 920;
const MIN_WIDTH = 1000;
const MIN_HEIGHT = 680;

/** Return the normal-state bounds Windows should restore to after maximize. */
export function defaultWindowBounds(workArea: DesktopWorkArea): DesktopWindowBounds {
  const width = Math.min(DEFAULT_WIDTH, Math.max(workArea.width, MIN_WIDTH));
  const height = Math.min(DEFAULT_HEIGHT, Math.max(workArea.height, MIN_HEIGHT));
  return {
    x: workArea.x + Math.max(0, Math.floor((workArea.width - width) / 2)),
    y: workArea.y + Math.max(0, Math.floor((workArea.height - height) / 2)),
    width,
    height,
  };
}
