import { useRef, useState } from "react";
import { Alert, Button, Modal } from "antd";
import {
  CheckCircleOutlined,
  DeleteOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  InboxOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import { userApi } from "../../api/client";
import { usePlatformTheme } from "../../theme/platform-theme";

const MAX_BYTES = 100 * 1024 * 1024;
const MAX_PARALLEL = 3;

type UploadStatus = "pending" | "uploading" | "done" | "error";

interface UploadTask {
  id: string;
  file: File;
  status: UploadStatus;
  message?: string;
}

interface Props {
  knowledgeBaseId: number;
  onClose: () => void;
  onUploaded: () => void;
  uploadFile?: (knowledgeBaseId: number, file: File) => Promise<unknown>;
}

function humanSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function taskId(file: File) {
  return `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`;
}

// 上传弹窗：antd Modal 壳 + 保留原上传逻辑（hidden input 选文件、100MB 预检、
// 最多 3 并发、失败可重试）。不引入 antd Upload，避免改变上传触发契约。
export function PersonalKnowledgeUploadDialog({ knowledgeBaseId, onClose, onUploaded, uploadFile = userApi.uploadPersonalKnowledgeFile }: Props) {
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { theme } = usePlatformTheme();

  const addFiles = (selected: FileList | File[]) => {
    const files = Array.from(selected);
    const oversized = files.filter((file) => file.size > MAX_BYTES);
    const accepted = files.filter((file) => file.size <= MAX_BYTES);
    setError(oversized.length ? `${oversized.map((file) => file.name).join("、")} 超过 100 MB，未加入上传队列` : null);
    if (accepted.length) {
      setTasks((current) => [
        ...current,
        ...accepted.map((file) => ({ id: taskId(file), file, status: "pending" as const }))
      ]);
    }
    if (inputRef.current) inputRef.current.value = "";
  };

  const updateTask = (id: string, patch: Partial<UploadTask>) => {
    setTasks((current) => current.map((task) => task.id === id ? { ...task, ...patch } : task));
  };

  const startUpload = async () => {
    const queue = tasks.filter((task) => task.status === "pending" || task.status === "error");
    if (!queue.length) return;
    setUploading(true);
    setError(null);
    let successCount = 0;
    let failureCount = 0;

    const runNext = async (): Promise<void> => {
      const task = queue.shift();
      if (!task) return;
      updateTask(task.id, { status: "uploading", message: undefined });
      try {
        await uploadFile(knowledgeBaseId, task.file);
        successCount += 1;
        updateTask(task.id, { status: "done" });
      } catch (reason) {
        failureCount += 1;
        updateTask(task.id, {
          status: "error",
          message: reason instanceof Error ? reason.message : "上传失败"
        });
      }
      await runNext();
    };

    await Promise.all(Array.from({ length: Math.min(MAX_PARALLEL, queue.length) }, () => runNext()));
    setUploading(false);
    if (successCount > 0) onUploaded();
    if (failureCount === 0) onClose();
    else setError(`${failureCount} 个文件上传失败，可以直接重试`);
  };

  const close = () => {
    if (!uploading) onClose();
  };

  const pendingCount = tasks.filter((task) => task.status === "pending" || task.status === "error").length;

  return (
    <Modal
      open
      title="上传文件"
      centered
      width={520}
      rootClassName={theme === "dark" ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
      className="pkbUploadModal"
      closable={!uploading}
      maskClosable={!uploading}
      keyboard={!uploading}
      onCancel={close}
      footer={
        <div className="pkbUploadFooter">
          <span>{tasks.length ? `已选择 ${tasks.length} 个文件` : "尚未选择文件"}</span>
          <div className="pkbUploadFooterActions">
            <Button autoInsertSpace={false} disabled={uploading} onClick={close}>取消</Button>
            <Button
              type="primary"
              autoInsertSpace={false}
              loading={uploading}
              disabled={!uploading && pendingCount === 0}
              onClick={() => void startUpload()}
            >{uploading ? "上传中…" : "开始上传"}</Button>
          </div>
        </div>
      }
    >
      <p className="pkbFormHint">文件上传后将由知识库服务自动解析</p>
      <label className={`pkbUploadDropzone ${uploading ? "disabled" : ""}`} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); if (!uploading) addFiles(event.dataTransfer.files); }}>
        <input ref={inputRef} className="srOnly" type="file" multiple aria-label="选择要上传的文件" disabled={uploading} onChange={(event) => addFiles(event.target.files || [])}/>
        <span className="pkbUploadDropIcon"><InboxOutlined aria-hidden /></span>
        <strong>点击或拖拽文件到这里</strong>
        <span>支持多文件，每个文件最大 100 MB，最多同时上传 3 个</span>
      </label>

      {error && <Alert className="pkbUploadError" type="error" showIcon message={error} />}

      {tasks.length > 0 && <div className="pkbUploadQueue" aria-label="待上传文件列表">
        {tasks.map((task) => <div className="pkbUploadTask" key={task.id}>
          <span className="pkbUploadFileIcon"><FileTextOutlined aria-hidden /></span>
          <div className="pkbUploadTaskCopy"><strong title={task.file.name}>{task.file.name}</strong><span>{humanSize(task.file.size)}{task.message ? ` · ${task.message}` : ""}</span></div>
          <span className={`pkbUploadTaskState ${task.status}`}>
            {task.status === "uploading" ? <><LoadingOutlined spin aria-hidden />上传中</> : task.status === "done" ? <><CheckCircleOutlined aria-hidden />已上传</> : task.status === "error" ? <><ExclamationCircleOutlined aria-hidden />失败</> : "等待上传"}
          </span>
          {task.status !== "uploading" && task.status !== "done" && <Button type="text" size="small" className="pkbDangerAction" aria-label={`移除 ${task.file.name}`} icon={<DeleteOutlined aria-hidden style={{ fontSize: 15, color: "#ff5722" }} />} disabled={uploading} onClick={() => setTasks((current) => current.filter((item) => item.id !== task.id))}/>}
        </div>)}
      </div>}
    </Modal>
  );
}
