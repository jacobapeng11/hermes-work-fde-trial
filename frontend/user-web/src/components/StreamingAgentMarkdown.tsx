import { memo, useRef } from "react";

import { AgentMarkdownContent } from "./AgentMarkdown";

type TopLevelKind = "paragraph" | "fenced" | "indented" | "table" | "list" | "blockquote";

type Fence = {
  marker: "`" | "~";
  length: number;
};

export type StreamingMarkdownScanState = {
  blocks: string[];
  scanned: string;
  settledLen: number;
  kind: TopLevelKind | null;
  fence: Fence | null;
  candidateLines: string[];
  pendingBoundary: number | null;
  pendingKind: Extract<TopLevelKind, "indented" | "table" | "list" | "blockquote"> | null;
};

const listMarker = /^\s{0,3}(?:[-+*]|\d+[.)])\s+/;
const blockquoteMarker = /^\s{0,3}>/;
const tableDivider = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;
const fenceMarker = /^\s{0,3}(`{3,}|~{3,})/;

export const createStreamingMarkdownScanState = (): StreamingMarkdownScanState => ({
  blocks: [],
  scanned: "",
  settledLen: 0,
  kind: null,
  fence: null,
  candidateLines: [],
  pendingBoundary: null,
  pendingKind: null,
});

function resetCandidate(state: StreamingMarkdownScanState): void {
  state.kind = null;
  state.candidateLines = [];
  state.pendingBoundary = null;
  state.pendingKind = null;
}

function commitThrough(
  text: string,
  state: StreamingMarkdownScanState,
  boundary: number,
): void {
  const block = text.slice(state.settledLen, boundary);
  if (/\S/.test(block)) state.blocks.push(block);
  state.settledLen = boundary;
  resetCandidate(state);
}

function continuesPendingStructure(kind: NonNullable<StreamingMarkdownScanState["pendingKind"]>, line: string): boolean {
  if (kind === "list") return listMarker.test(line) || /^\s{2,}\S/.test(line);
  if (kind === "blockquote") return blockquoteMarker.test(line);
  if (kind === "indented") return /^\s{4,}\S/.test(line);
  return line.includes("|");
}

function applyNonBlankLine(text: string, state: StreamingMarkdownScanState, line: string): void {
  if (state.pendingBoundary !== null && state.pendingKind !== null) {
    if (continuesPendingStructure(state.pendingKind, line)) {
      state.pendingBoundary = null;
      state.pendingKind = null;
    } else {
      commitThrough(text, state, state.pendingBoundary);
    }
  }

  const markerMatch = fenceMarker.exec(line);
  if (state.fence !== null) {
    if (
      markerMatch
      && markerMatch[1][0] === state.fence.marker
      && markerMatch[1].length >= state.fence.length
      && line.slice(markerMatch[0].length).trim() === ""
    ) {
      state.fence = null;
    }
    state.kind = "fenced";
    if (state.candidateLines.length < 2) state.candidateLines.push(line);
    return;
  }

  if (markerMatch) {
    state.fence = {
      marker: markerMatch[1][0] as Fence["marker"],
      length: markerMatch[1].length,
    };
    state.kind = "fenced";
    if (state.candidateLines.length < 2) state.candidateLines.push(line);
    return;
  }

  if (state.kind === null) {
    state.kind = listMarker.test(line)
      ? "list"
      : blockquoteMarker.test(line)
        ? "blockquote"
        : /^\s{4,}\S/.test(line)
          ? "indented"
          : "paragraph";
  }
  const completesTableHead = state.kind === "paragraph" && state.candidateLines.length === 1;
  if (state.candidateLines.length < 2) state.candidateLines.push(line);
  if (
    completesTableHead
    && state.candidateLines[0].includes("|")
    && tableDivider.test(line)
  ) {
    state.kind = "table";
  }
}

export function advanceStreamingMarkdownScan(
  text: string,
  state: StreamingMarkdownScanState,
): void {
  const start = state.scanned.length;
  let cursor = start;

  while (cursor < text.length) {
    const newline = text.indexOf("\n", cursor);
    if (newline < 0) break;
    const line = text.slice(cursor, newline);

    if (/^\s*$/.test(line)) {
      if (state.fence === null && state.kind !== null) {
        if (
          state.kind === "indented"
          || state.kind === "list"
          || state.kind === "blockquote"
          || state.kind === "table"
        ) {
          state.pendingBoundary = newline + 1;
          state.pendingKind = state.kind;
        } else {
          commitThrough(text, state, newline + 1);
        }
      } else if (state.pendingBoundary !== null) {
        state.pendingBoundary = newline + 1;
      }
    } else {
      applyNonBlankLine(text, state, line);
    }
    cursor = newline + 1;
  }

  if (cursor > start) state.scanned += text.slice(start, cursor);
}

export function stableStreamingMarkdownBoundary(text: string): number {
  const state = createStreamingMarkdownScanState();
  advanceStreamingMarkdownScan(text, state);
  return state.settledLen > 0 ? state.settledLen : -1;
}

export const StreamingAgentMarkdown = memo(function StreamingAgentMarkdown({ content }: { content: string }) {
  const scanRef = useRef<StreamingMarkdownScanState>(createStreamingMarkdownScanState());
  let state = scanRef.current;

  if (!content.startsWith(state.scanned)) {
    state = scanRef.current = createStreamingMarkdownScanState();
  }
  advanceStreamingMarkdownScan(content, state);
  const tail = content.slice(state.settledLen);

  return (
    <div className="agentMarkdown" data-streaming-markdown="true">
      {state.blocks.map((block, index) => (
        <AgentMarkdownContent content={block} key={`block:${index}`}/>
      ))}
      {tail ? <AgentMarkdownContent content={tail} key="tail"/> : null}
    </div>
  );
});
