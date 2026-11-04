export interface CollapsibleSessionRow {
  id: string;
  height: number;
  priority: boolean;
}

export interface CollapsibleSessionLayout {
  overflow: boolean;
  visibleIds: string[];
}

const ROW_GAP_PX = 1;

export function selectCollapsedSessions(
  rows: CollapsibleSessionRow[],
  availableHeight: number,
  expandControlHeight = 34
): CollapsibleSessionLayout {
  const totalHeight = rows.reduce((sum, row) => sum + row.height, 0)
    + Math.max(0, rows.length - 1) * ROW_GAP_PX;
  if (availableHeight <= 0 || totalHeight <= availableHeight) {
    return { overflow: false, visibleIds: rows.map((row) => row.id) };
  }

  const capacity = Math.max(0, availableHeight - expandControlHeight);
  const selected = new Set<string>();
  let used = 0;
  const addIfFits = (row: CollapsibleSessionRow) => {
    const nextHeight = row.height + (selected.size > 0 ? ROW_GAP_PX : 0);
    if (used + nextHeight > capacity && selected.size > 0) return;
    selected.add(row.id);
    used += nextHeight;
  };

  rows.filter((row) => row.priority).forEach(addIfFits);
  rows.filter((row) => !row.priority).forEach(addIfFits);
  return {
    overflow: true,
    visibleIds: rows.filter((row) => selected.has(row.id)).map((row) => row.id)
  };
}
