import { useEffect, useRef, useState } from "react";
import { Download, FileWarning, LoaderCircle, X } from "lucide-react";
import type { ChatFileSummary } from "@hermes-cloud/schemas";

export type FilePreviewKind = "image" | "pdf" | "text" | "audio" | "video" | "office" | "unsupported";

const TEXT_EXTENSIONS = new Set([
  "txt", "md", "markdown", "json", "csv", "tsv", "log", "xml", "yaml", "yml",
  "html", "css", "js", "jsx", "ts", "tsx", "py", "sh", "sql", "toml", "ini", "conf"
]);
const OFFICE_EXTENSIONS = new Set([
  "doc", "docm", "docx", "dot", "dotm", "dotx", "odt", "ott", "rtf",
  "xls", "xlsb", "xlsm", "xlsx", "xlt", "xltm", "xltx", "ods", "ots",
  "ppt", "pptm", "pptx", "pps", "ppsm", "ppsx", "pot", "potm", "potx", "odp", "otp"
]);

export function filePreviewKind(file: Pick<ChatFileSummary, "displayName" | "mimeType">, blobType = ""): FilePreviewKind {
  const mimeType = (blobType || file.mimeType || "").toLowerCase().split(";", 1)[0];
  const extension = file.displayName.toLowerCase().split(".").pop() || "";
  if (mimeType.startsWith("image/")) return "image";
  if (mimeType === "application/pdf" || extension === "pdf") return "pdf";
  if (mimeType.startsWith("audio/")) return "audio";
  if (mimeType.startsWith("video/")) return "video";
  if (OFFICE_EXTENSIONS.has(extension)) return "office";
  if (mimeType.startsWith("text/") || mimeType === "application/json" || mimeType.endsWith("+json") || TEXT_EXTENSIONS.has(extension)) return "text";
  return "unsupported";
}

export interface FilePreviewState {
  file: ChatFileSummary;
  status: "loading" | "ready" | "error";
  kind?: FilePreviewKind;
  url?: string;
  text?: string;
  truncated?: boolean;
  error?: string;
  office?: { documentServerUrl: string; config: Record<string, unknown> };
}

interface FilePreviewDialogProps {
  preview: FilePreviewState;
  onClose: () => void;
  onDownload: (file: ChatFileSummary) => void;
  canDownload?: boolean;
}

interface OnlyOfficeEditor {
  destroyEditor?: () => void;
}

declare global {
  interface Window {
    DocsAPI?: { DocEditor: new (elementId: string, config: Record<string, unknown>) => OnlyOfficeEditor };
  }
}

function OnlyOfficeViewer({ documentServerUrl, config }: { documentServerUrl: string; config: Record<string, unknown> }) {
  const containerId = useRef(`onlyoffice-viewer-${Math.random().toString(36).slice(2)}`);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let editor: OnlyOfficeEditor | null = null;
    let disposed = false;
    const scriptUrl = `${documentServerUrl.replace(/\/$/, "")}/web-apps/apps/api/documents/api.js`;
    const startEditor = () => {
      if (disposed) return;
      if (!window.DocsAPI?.DocEditor) {
        setError("ONLYOFFICE API 加载失败");
        return;
      }
      try {
        editor = new window.DocsAPI.DocEditor(containerId.current, { ...config, width: "100%", height: "100%" });
        setReady(true);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "ONLYOFFICE 初始化失败");
      }
    };

    if (window.DocsAPI?.DocEditor) {
      startEditor();
    } else {
      let script = document.querySelector<HTMLScriptElement>(`script[data-onlyoffice-api="${scriptUrl}"]`);
      if (!script) {
        script = document.createElement("script");
        script.src = scriptUrl;
        script.async = true;
        script.dataset.onlyofficeApi = scriptUrl;
        document.head.appendChild(script);
      }
      script.addEventListener("load", startEditor);
      script.addEventListener("error", () => !disposed && setError("无法连接 ONLYOFFICE 文档服务"), { once: true });
    }
    return () => {
      disposed = true;
      editor?.destroyEditor?.();
    };
  }, [config, documentServerUrl]);

  return <div className="onlyOfficePreview"><div id={containerId.current}/>{!ready && !error && <div className="filePreviewStatus"><LoaderCircle className="uploadSpinner" size={22}/><span>正在启动 Office 预览…</span></div>}{error && <div className="filePreviewStatus"><FileWarning size={25}/><strong>Office 预览加载失败</strong><span>{error}</span></div>}</div>;
}

export function FilePreviewDialog({ preview, onClose, onDownload, canDownload = true }: FilePreviewDialogProps) {
  const { file } = preview;
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);
  return <div className="filePreviewBackdrop" role="presentation" onMouseDown={onClose}>
    <section className="filePreviewDialog" role="dialog" aria-modal="true" aria-labelledby="file-preview-title" onMouseDown={(event) => event.stopPropagation()}>
      <header className="filePreviewHeader">
        <div><h2 id="file-preview-title" title={file.displayName}>{file.displayName}</h2><small>{file.mimeType || "未知文件类型"}</small></div>
        <div className="filePreviewActions">
          {canDownload && <button type="button" onClick={() => onDownload(file)} title="下载文件" aria-label={`下载 ${file.displayName}`}><Download size={17}/></button>}
          <button type="button" onClick={onClose} title="关闭预览" aria-label="关闭文件预览"><X size={18}/></button>
        </div>
      </header>
      <div className="filePreviewBody" data-kind={preview.kind || "loading"}>
        {preview.status === "loading" && <div className="filePreviewStatus"><LoaderCircle className="uploadSpinner" size={22}/><span>正在加载预览…</span></div>}
        {preview.status === "error" && <div className="filePreviewStatus"><FileWarning size={25}/><strong>无法加载预览</strong><span>{preview.error}</span>{canDownload && <button type="button" onClick={() => onDownload(file)}><Download size={15}/>下载文件</button>}</div>}
        {preview.status === "ready" && preview.kind === "image" && <img src={preview.url} alt={file.displayName}/>}
        {preview.status === "ready" && preview.kind === "pdf" && <iframe src={preview.url} title={file.displayName}/>}
        {preview.status === "ready" && preview.kind === "audio" && <audio src={preview.url} controls/>}
        {preview.status === "ready" && preview.kind === "video" && <video src={preview.url} controls/>}
        {preview.status === "ready" && preview.kind === "office" && preview.office && <OnlyOfficeViewer documentServerUrl={preview.office.documentServerUrl} config={preview.office.config}/>}
        {preview.status === "ready" && preview.kind === "text" && <div className="fileTextPreview">{preview.truncated && <div className="filePreviewLimit">文件较大，仅显示前 1 MB</div>}<pre>{preview.text}</pre></div>}
        {preview.status === "ready" && preview.kind === "unsupported" && <div className="filePreviewStatus"><FileWarning size={25}/><strong>该格式暂不支持在线预览</strong><span>{canDownload ? "请下载后使用本地应用打开。" : "当前权限不允许下载原文件。"}</span>{canDownload && <button type="button" onClick={() => onDownload(file)}><Download size={15}/>下载文件</button>}</div>}
      </div>
    </section>
  </div>;
}
