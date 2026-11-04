export function extractHtmlArtifacts(markdown: string): string[] {
  if (!markdown) return [];
  const artifacts: string[] = [];
  const lines = markdown.split(/\r?\n/);
  let fenceMarker = "";
  let fenceLength = 0;
  let inHtmlBlock = false;
  let block: string[] = [];

  for (const line of lines) {
    const match = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (!inHtmlBlock && match) {
      const language = match[2].trim().toLowerCase();
      if (language === "html") {
        fenceMarker = match[1][0];
        fenceLength = match[1].length;
        inHtmlBlock = true;
        block = [];
      }
      continue;
    }
    if (inHtmlBlock && match) {
      const closing = match[1];
      if (
        closing[0] === fenceMarker
        && closing.length >= fenceLength
        && match[2].trim() === ""
      ) {
        const html = block.join("\n");
        if (isCompleteHtmlDocument(html)) artifacts.push(html);
        inHtmlBlock = false;
        block = [];
        continue;
      }
    }
    if (inHtmlBlock) block.push(line);
  }
  return artifacts;
}

export function isCompleteHtmlDocument(html: string): boolean {
  return /<html\b|<!doctype\s+html/i.test(html);
}

export function htmlDocumentTitle(html: string): string | null {
  const match = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  if (!match) return null;
  const title = match[1].replace(/\s+/g, " ").trim();
  return title || null;
}

// 预览关闭屏蔽按「内容签名」记录：同一份内容不再自动重开，
// 但 agent 更新 HTML 后签名变化，预览会再次自动弹出。
export function htmlPreviewSignature(sourceKey: string, html: string): string {
  let hash = 0;
  for (let index = 0; index < html.length; index += 1) {
    hash = (hash * 31 + html.charCodeAt(index)) | 0;
  }
  return `${sourceKey}:${html.length}:${hash}`;
}
