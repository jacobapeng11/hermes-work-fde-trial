import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Divider, Empty, Flex, Space, Tag, Typography } from "antd";
import {
  ArrowLeftOutlined,
  DatabaseOutlined,
  EditOutlined,
  FileTextOutlined,
  SafetyCertificateOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { ChatFileSummary, PersonalKnowledgeDetail as Detail, PersonalKnowledgeFileSummary, PersonalKnowledgeRole } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import {
  formatTime,
  humanSize,
  PersonalKnowledgeFileRowActions,
  PersonalKnowledgeFileTable,
  fileStatusTag,
} from "./PersonalKnowledgeFileTable";
import { PersonalKnowledgeFileDeleteDialog, PersonalKnowledgeFileRenameDialog } from "./PersonalKnowledgeFileActionDialogs";
import { PersonalKnowledgeFormDialog } from "./PersonalKnowledgeFormDialog";
import { PersonalKnowledgePermissionsDialog } from "./PersonalKnowledgePermissionsDialog";
import { PersonalKnowledgeUploadDialog } from "./PersonalKnowledgeUploadDialog";
import { FilePreviewDialog, filePreviewKind, type FilePreviewState } from "../FilePreviewDialog";
import type { PersonalKnowledgeAssetApi } from "./personalKnowledgeAssetApi";

const MAX_TEXT_PREVIEW_BYTES = 1024 * 1024;
const { Title } = Typography;

function readBlobText(blob: Blob): Promise<string> {
  if (typeof blob.text === "function") return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(typeof reader.result === "string" ? reader.result : ""));
    reader.addEventListener("error", () => reject(reader.error || new Error("文件读取失败")));
    reader.readAsText(blob);
  });
}

function previewFileShape(knowledgeBaseId: number, file: PersonalKnowledgeFileSummary): ChatFileSummary {
  return {
    fileId: file.fileId,
    runtimeWorkspaceId: knowledgeBaseId,
    relativePath: `personal-knowledge/${knowledgeBaseId}/${file.filename}`,
    displayName: file.filename,
    mimeType: file.mimeType,
    sizeBytes: file.sizeBytes,
    sourceType: "personal_knowledge",
    createdAt: file.updatedAt
  };
}

interface Props {
  knowledgeBaseId: number;
  onBack: (changed?: boolean) => void;
  assetApi?: PersonalKnowledgeAssetApi;
  assetLabel?: string;
}

const roleLabels: Record<PersonalKnowledgeRole, string> = {
  owner: "我创建的",
  download: "可预览和下载",
  preview: "仅在线预览",
  retrieve: "仅勾选检索"
};

const roleColor: Record<PersonalKnowledgeRole, "blue" | "green" | "default" | "gold"> = {
  owner: "blue",
  download: "green",
  preview: "default",
  retrieve: "gold"
};

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function PersonalKnowledgeDetail({ knowledgeBaseId, onBack, assetApi, assetLabel = "个人知识库" }: Props) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [files, setFiles] = useState<PersonalKnowledgeFileSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [renaming, setRenaming] = useState<PersonalKnowledgeFileSummary | null>(null);
  const [deleting, setDeleting] = useState<PersonalKnowledgeFileSummary | null>(null);
  const [filePreview, setFilePreview] = useState<FilePreviewState | null>(null);
  const detailRequestSequenceRef = useRef(0);
  const filesRequestSequenceRef = useRef(0);
  const previewRequestRef = useRef(0);
  const previewUrlRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    const detailRequestSequence = ++detailRequestSequenceRef.current;
    const filesRequestSequence = ++filesRequestSequenceRef.current;
    setLoading(true);
    setMessage(null);
    try {
      const [nextDetail, nextFiles] = await Promise.all([
        assetApi ? assetApi.detail(knowledgeBaseId) : userApi.personalKnowledgeDetail(knowledgeBaseId),
        assetApi ? assetApi.files(knowledgeBaseId) : userApi.personalKnowledgeFiles(knowledgeBaseId)
      ]);
      if (detailRequestSequence !== detailRequestSequenceRef.current) return;
      setDetail(nextDetail);
      if (filesRequestSequence === filesRequestSequenceRef.current) setFiles(nextFiles.items);
    } catch (reason) {
      if (detailRequestSequence === detailRequestSequenceRef.current) {
        setMessage(reason instanceof Error ? reason.message : "读取知识库失败");
      }
    } finally {
      if (detailRequestSequence === detailRequestSequenceRef.current) setLoading(false);
    }
  }, [assetApi, knowledgeBaseId]);

  const refreshFiles = useCallback(async () => {
    const requestSequence = ++filesRequestSequenceRef.current;
    try {
      const response = assetApi
        ? await assetApi.files(knowledgeBaseId)
        : await userApi.personalKnowledgeFiles(knowledgeBaseId);
      if (requestSequence === filesRequestSequenceRef.current) setFiles(response.items);
    } catch {
      // File parsing polls are best effort; preserve the existing list until
      // the next successful read rather than replacing it with an error state.
    }
  }, [assetApi, knowledgeBaseId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => {
    detailRequestSequenceRef.current += 1;
    filesRequestSequenceRef.current += 1;
    previewRequestRef.current += 1;
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
  }, []);
  useEffect(() => {
    if (!files.some((file) => file.status === "pending" || file.status === "parsing")) return;
    const timer = window.setInterval(() => {
      void refreshFiles();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [files, refreshFiles]);

  const preview = async (file: PersonalKnowledgeFileSummary) => {
    const requestId = previewRequestRef.current + 1;
    previewRequestRef.current = requestId;
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
    const previewFile = previewFileShape(knowledgeBaseId, file);
    setFilePreview({ file: previewFile, status: "loading" });
    try {
      if (filePreviewKind(previewFile) === "office") {
        const office = assetApi?.officePreviewSession
          ? await assetApi.officePreviewSession(knowledgeBaseId, file.fileId)
          : !assetApi
            ? await userApi.personalKnowledgeOfficePreviewSession(knowledgeBaseId, file.fileId)
            : null;
        if (office === null) {
          setFilePreview({ file: previewFile, status: "ready", kind: "unsupported" });
          return;
        }
        if (previewRequestRef.current === requestId) {
          setFilePreview({ file: previewFile, status: "ready", kind: "office", office });
        }
        return;
      }
      const blob = assetApi
        ? await assetApi.fileContent(knowledgeBaseId, file.fileId, false)
        : await userApi.personalKnowledgeFileContent(knowledgeBaseId, file.fileId, false);
      if (previewRequestRef.current !== requestId) return;
      const kind = filePreviewKind(previewFile, blob.type);
      if (kind === "text") {
        const truncated = blob.size > MAX_TEXT_PREVIEW_BYTES;
        const text = await readBlobText(blob.slice(0, MAX_TEXT_PREVIEW_BYTES));
        if (previewRequestRef.current === requestId) {
          setFilePreview({ file: previewFile, status: "ready", kind, text, truncated });
        }
        return;
      }
      if (kind === "unsupported") {
        setFilePreview({ file: previewFile, status: "ready", kind });
        return;
      }
      const url = URL.createObjectURL(blob);
      previewUrlRef.current = url;
      setFilePreview({ file: previewFile, status: "ready", kind, url });
    } catch (reason) {
      if (previewRequestRef.current === requestId) {
        setFilePreview({ file: previewFile, status: "error", error: reason instanceof Error ? reason.message : "文件预览失败" });
      }
    }
  };

  const closePreview = () => {
    previewRequestRef.current += 1;
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
    setFilePreview(null);
  };

  const download = async (file: PersonalKnowledgeFileSummary) => {
    try {
      saveBlob(assetApi
        ? await assetApi.fileContent(knowledgeBaseId, file.fileId, true)
        : await userApi.personalKnowledgeFileContent(knowledgeBaseId, file.fileId, true), file.filename);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "文件下载失败");
    }
  };

  if (loading && !detail) return <div className="pkbDetail"><p className="pkbDetailLoading" role="status">正在读取个人知识库…</p></div>;
  if (!detail) return <div className="pkbDetail"><Button type="text" className="pkbBack pkbDetailBack" icon={<ArrowLeftOutlined aria-hidden />} onClick={() => onBack()}>返回个人知识库</Button><p className="pkbError">{message || "知识库不可用"}</p><Button autoInsertSpace={false} onClick={() => void load()}>重试</Button></div>;

  const rowActions = (file: PersonalKnowledgeFileSummary) => <PersonalKnowledgeFileRowActions
    file={file}
    canDownload={detail.canDownload}
    canManage={detail.canManage}
    onPreview={(target) => void preview(target)}
    onDownload={(target) => void download(target)}
    onRename={setRenaming}
    onDelete={setDeleting}
  />;

  return <section className="pkbDetail">
    <Button
      type="text"
      className="pkbBack pkbDetailBack"
      style={{ marginBottom: 16 }}
      icon={<ArrowLeftOutlined aria-hidden />}
      onClick={() => onBack()}
    >返回个人知识库</Button>
    <Flex vertical gap={24}>
      <Card className="pkbDetailCard" styles={{ body: { padding: 24 } }}>
        <Flex justify="space-between" align="flex-start" gap={16}>
          <Flex vertical style={{ flex: 1, minWidth: 0 }}>
            <Flex align="center" gap={12} wrap="wrap">
              <span className="pkbDetailTitleIcon"><DatabaseOutlined aria-hidden /></span>
              <Title level={4} className="pkbDetailTitle" style={{ margin: 0 }}>{detail.name}</Title>
              <Tag color={roleColor[detail.effectiveRole]}>{roleLabels[detail.effectiveRole]}</Tag>
              {detail.canManage && <Button type="text" size="small" aria-label="编辑知识库" icon={<EditOutlined aria-hidden />} onClick={() => setEditing(true)}>编辑</Button>}
            </Flex>
            <p className="pkbDetailDescription">{detail.description || "暂无描述"}</p>
            <Space split={<Divider type="vertical" />} wrap className="pkbDetailMeta">
              <span><span className="pkbDetailMetaLabel">创建者：</span>{detail.ownerName}</span>
              <span>{files.length} 个文件</span>
              <span>{files.filter((file) => file.status === "ready").length} 个可检索</span>
            </Space>
          </Flex>
          {detail.canManage && (
            <Space>
              {!assetApi && <Button autoInsertSpace={false} icon={<SafetyCertificateOutlined aria-hidden />} onClick={() => setSharing(true)}>权限设置</Button>}
              <Button type="primary" autoInsertSpace={false} icon={<UploadOutlined aria-hidden />} onClick={() => setUploadOpen(true)}>上传文件</Button>
            </Space>
          )}
        </Flex>
      </Card>

      {message && <Alert className="pkbDetailMessage" type="error" showIcon message={message} closable onClose={() => setMessage(null)} />}

      <Card
        className="pkbFilesCard"
        styles={{ body: { padding: 0 } }}
        title={<Flex vertical>
          <span className="pkbFilesCardTitle" id="pkb-files-title">知识库文件</span>
          <span className="pkbFilesCardSubtitle">文件解析完成后即可被勾选的个人知识库检索</span>
        </Flex>}
        extra={<span className="pkbFilesCount">{files.length} 个文件</span>}
      >
        {files.length > 0 ? <>
          <div className="pkbFileTableWrap">
            <PersonalKnowledgeFileTable
              files={files}
              canDownload={detail.canDownload}
              canManage={detail.canManage}
              onPreview={(file) => void preview(file)}
              onDownload={(file) => void download(file)}
              onRename={setRenaming}
              onDelete={setDeleting}
            />
          </div>
          <div className="pkbFileCards">{files.map((file) => <article className="pkbFileCard" key={file.fileId}>
            <div className="pkbFileCardTop"><span className="pkbUploadFileIcon"><FileTextOutlined aria-hidden /></span><button type="button" className="pkbFileLink" aria-label={`预览 ${file.filename}`} onClick={() => void preview(file)}>{file.filename}</button></div>
            <div className="pkbFileCardMeta"><span>{humanSize(file.sizeBytes)}</span>{fileStatusTag(file.status)}<span>{file.uploaderName || `#${file.uploaderId}`} · {formatTime(file.updatedAt)}</span></div>
            {rowActions(file)}
          </article>)}</div>
        </> : (
          <Empty
            className="pkbFilesEmpty"
            description={<Flex vertical>
              <span className="pkbFilesEmptyTitle">还没有文件</span>
              <span className="pkbFilesEmptyText">{detail.canManage ? "上传自己的资料，外部服务解析完成后即可检索。" : "创建者暂未上传文件。"}</span>
            </Flex>}
          >
            {detail.canManage && <Button autoInsertSpace={false} icon={<UploadOutlined aria-hidden />} onClick={() => setUploadOpen(true)}>上传第一个文件</Button>}
          </Empty>
        )}
      </Card>
    </Flex>

    {sharing && <PersonalKnowledgePermissionsDialog knowledgeBaseId={knowledgeBaseId} knowledgeBaseName={detail.name} onClose={() => setSharing(false)}/>}
    {uploadOpen && (
      <PersonalKnowledgeUploadDialog
        knowledgeBaseId={knowledgeBaseId}
        uploadFile={assetApi?.uploadFile}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => { void load(); }}
      />
    )}
    {editing && (
      <PersonalKnowledgeFormDialog
        title="编辑知识库"
        supportingText="更新知识库名称与说明，不会影响现有文件"
        submitLabel="保存修改"
        pendingLabel="保存中…"
        initialName={detail.name}
        initialDescription={detail.description}
        onClose={() => setEditing(false)}
        onSubmit={async (value) => {
          if (assetApi) await assetApi.update(knowledgeBaseId, value);
          else await userApi.updatePersonalKnowledge(knowledgeBaseId, value);
          await load();
        }}
      />
    )}
    {renaming && (
      <PersonalKnowledgeFileRenameDialog
        key={renaming.fileId}
        filename={renaming.filename}
        onClose={() => setRenaming(null)}
        onRename={async (filename) => {
          if (assetApi) await assetApi.renameFile(knowledgeBaseId, renaming.fileId, filename);
          else await userApi.renamePersonalKnowledgeFile(knowledgeBaseId, renaming.fileId, filename);
          await load();
        }}
      />
    )}
    {deleting && <PersonalKnowledgeFileDeleteDialog
      key={deleting.fileId}
      filename={deleting.filename}
      onClose={() => setDeleting(null)}
      onDelete={async () => {
        if (assetApi) await assetApi.deleteFile(knowledgeBaseId, deleting.fileId);
        else await userApi.deletePersonalKnowledgeFile(knowledgeBaseId, deleting.fileId);
        ++filesRequestSequenceRef.current;
        setFiles((current) => current.filter((item) => item.fileId !== deleting.fileId));
      }}
    />}
    {filePreview && <FilePreviewDialog preview={filePreview} onClose={closePreview} canDownload={detail.canDownload} onDownload={(previewFile) => {
      const source = files.find((item) => item.fileId === previewFile.fileId);
      if (source && detail.canDownload) void download(source);
    }}/>}
  </section>;
}
