import { useEffect, useRef, useState } from "react";
import { Check, Download, Eye, FileText, MoreHorizontal, Pencil, Trash2, Upload, X } from "lucide-react";

export interface FileSummary {
  fileId: number;
  displayName: string;
  sourceType: string;
  sizeBytes: number;
}

function formatSize(sizeBytes: number): string {
  if (sizeBytes >= 1024 * 1024) return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
  return sizeBytes >= 1024 ? `${Math.round(sizeBytes / 1024)} KB` : `${sizeBytes} B`;
}

const sourceLabel = (sourceType: string) => sourceType === "upload" ? "用户上传" : sourceType === "export" ? "导出文件" : "Agent 生成";

interface FileRailProps {
  files: FileSummary[];
  pendingFileIds?: number[];
  onClose: () => void;
  onUpload?: () => void;
  onPreview?: (file: FileSummary) => void;
  onDownload?: (file: FileSummary) => void;
  onRename?: (file: FileSummary, displayName: string) => void;
  onDelete?: (file: FileSummary) => void;
  readOnly?: boolean;
}

export function FileRail({ files, pendingFileIds = [], onClose, onUpload, onPreview, onDownload, onRename, onDelete, readOnly = false }: FileRailProps) {
  const railRef = useRef<HTMLElement | null>(null);
  const [menuFileId, setMenuFileId] = useState<number | null>(null);
  const [renamingFileId, setRenamingFileId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const pending = files.filter((file) => pendingFileIds.includes(file.fileId));
  const uploaded = files.filter((file) => file.sourceType === "upload" && !pendingFileIds.includes(file.fileId));
  const generated = files.filter((file) => file.sourceType !== "upload" && !pendingFileIds.includes(file.fileId));

  const beginRename = (file: FileSummary) => {
    setMenuFileId(null);
    setRenamingFileId(file.fileId);
    setRenameValue(file.displayName);
  };
  useEffect(() => {
    if (menuFileId === null) return;
    const closeOnOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && railRef.current?.contains(target)) return;
      setMenuFileId(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuFileId(null);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [menuFileId]);
  const finishRename = (file: FileSummary) => {
    const next = renameValue.trim();
    if (next && next !== file.displayName) onRename?.(file, next);
    setRenamingFileId(null);
  };
  const renderGroup = (label: string, items: FileSummary[]) => items.length > 0 && <section className="fileGroup" aria-label={label}>
    <h3>{label}<small>{items.length}</small></h3>
    <div className="fileList">{items.map((file) => <article className="fileCard" key={file.fileId}>
      <button type="button" className="fileOpen" onClick={() => onPreview?.(file)} aria-label={`预览 ${file.displayName}`}><span className="fileTypeIcon"><FileText size={16}/></span><span>{renamingFileId === file.fileId ? <span className="fileRename" onClick={(event) => event.stopPropagation()}><input autoFocus aria-label={`重命名 ${file.displayName}`} value={renameValue} onChange={(event) => setRenameValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") finishRename(file); if (event.key === "Escape") setRenamingFileId(null); }}/><button type="button" aria-label="保存文件名" onClick={() => finishRename(file)}><Check size={13}/></button><button type="button" aria-label="取消重命名" onClick={() => setRenamingFileId(null)}><X size={13}/></button></span> : <strong title={file.displayName}>{file.displayName}</strong>}<small>{sourceLabel(file.sourceType)} · {formatSize(file.sizeBytes)}</small></span></button>
      <button type="button" className="fileMenuButton" aria-label={`文件操作 ${file.displayName}`} aria-expanded={menuFileId === file.fileId} onClick={() => setMenuFileId((current) => current === file.fileId ? null : file.fileId)}><MoreHorizontal size={15}/></button>
      {menuFileId === file.fileId && <div className="fileMenu">
        <button type="button" onClick={() => { setMenuFileId(null); onPreview?.(file); }}><Eye size={15}/>预览</button>
        <button type="button" onClick={() => { setMenuFileId(null); onDownload?.(file); }}><Download size={15}/>下载</button>
        {!readOnly && <button type="button" onClick={() => beginRename(file)}><Pencil size={15}/>重命名</button>}
        {!readOnly && <button type="button" className="dangerAction" onClick={() => { setMenuFileId(null); onDelete?.(file); }}><Trash2 size={15}/>删除</button>}
      </div>}
    </article>)}</div>
  </section>;

  return <aside ref={railRef} className="filesPane" aria-label="当前任务文件">
    <div className="filesTitle"><span>当前任务文件</span><small>{files.length}</small><button type="button" className="filesClose" onClick={onClose} aria-label="关闭当前任务文件" title="关闭当前任务文件"><X size={17}/></button></div>
    {!readOnly && <button type="button" className="fileUploadButton" aria-label="添加文件" onClick={onUpload}>
      <span className="fileUploadIcon" aria-hidden="true"><Upload size={18}/></span>
      <span className="fileUploadText">点击或拖拽文件上传<small>支持 PDF, Word, Excel, TXT</small></span>
    </button>}
    {renderGroup("本次待发送", pending)}
    {renderGroup("用户上传", uploaded)}
    {renderGroup("Agent 生成", generated)}
    {files.length === 0 && <p className="fileNote">上传资料或让 Agent 创建文件，文件会保存在当前任务工作区。</p>}
  </aside>;
}
