import type { CSSProperties } from "react";
import { Button, Flex, Table, Tag } from "antd";
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
} from "@ant-design/icons";
import type { PersonalKnowledgeFileSummary } from "@hermes-cloud/schemas";

const statusLabels = {
  pending: "等待解析",
  parsing: "解析中",
  ready: "可检索",
  failed: "解析失败"
} as const;

const statusColor = {
  pending: "warning",
  parsing: "processing",
  ready: "success",
  failed: "error"
} as const;

export function humanSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export function formatTime(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

// 解析状态：antd 语义色 Tag（warning=等待 / processing=解析中 / success=可检索 / error=失败）。
export function fileStatusTag(status: PersonalKnowledgeFileSummary["status"]) {
  return <Tag color={statusColor[status]}>{statusLabels[status]}</Tag>;
}

interface RowActionsProps {
  file: PersonalKnowledgeFileSummary;
  canDownload: boolean;
  canManage: boolean;
  onPreview: (file: PersonalKnowledgeFileSummary) => void;
  onDownload: (file: PersonalKnowledgeFileSummary) => void;
  onRename: (file: PersonalKnowledgeFileSummary) => void;
  onDelete: (file: PersonalKnowledgeFileSummary) => void;
}

// 桌面表格与移动卡片共用的文件操作按钮组（aria-label 契约保持）。
export function PersonalKnowledgeFileRowActions({
  file,
  canDownload,
  canManage,
  onPreview,
  onDownload,
  onRename,
  onDelete
}: RowActionsProps) {
  return <div className="pkbRowActions">
    <Button type="text" size="small" title="在线预览" aria-label={`预览 ${file.filename}`} icon={<EyeOutlined aria-hidden />} onClick={() => onPreview(file)} />
    {canDownload && <Button type="text" size="small" title="下载" aria-label={`下载 ${file.filename}`} icon={<DownloadOutlined aria-hidden />} onClick={() => onDownload(file)} />}
    {canManage && <>
      <Button type="text" size="small" title="重命名" aria-label={`重命名 ${file.filename}`} icon={<EditOutlined aria-hidden />} onClick={() => onRename(file)} />
      <Button type="text" size="small" className="pkbDangerAction" title="永久删除" aria-label={`删除 ${file.filename}`} icon={<DeleteOutlined aria-hidden style={{ fontSize: 15, color: "#ff5722" }} />} onClick={() => onDelete(file)} />
    </>}
  </div>;
}

interface FileTableProps {
  files: PersonalKnowledgeFileSummary[];
  canDownload: boolean;
  canManage: boolean;
  onPreview: (file: PersonalKnowledgeFileSummary) => void;
  onDownload: (file: PersonalKnowledgeFileSummary) => void;
  onRename: (file: PersonalKnowledgeFileSummary) => void;
  onDelete: (file: PersonalKnowledgeFileSummary) => void;
}

// 桌面文件表（antd Table，协作平台 el-table 基线）：白底、表头浅灰、行 hover、轻边框。
// 窄屏（≤760px）由 CSS 切换为移动卡片形态（见 PersonalKnowledgeDetail）。
export function PersonalKnowledgeFileTable({
  files,
  canDownload,
  canManage,
  onPreview,
  onDownload,
  onRename,
  onDelete
}: FileTableProps) {
  // 单元格内边距经 onHeaderCell/onCell 以 inline style 下发（24px 与 Card 标题对齐）；
  // inline style 优先级高于 antd CSS-in-JS 运行时样式，避免覆盖失效导致的错位。
  const headerCell = () => ({ style: { padding: "12px 24px" } as CSSProperties });
  const bodyCell = () => ({ style: { padding: "16px 24px" } as CSSProperties });
  return (
    <Table
      className="pkbFileTable"
      rowKey="fileId"
      size="middle"
      showSorterTooltip={false}
      pagination={false}
      dataSource={files}
      columns={[
        {
          title: "文件名称",
          dataIndex: "filename",
          onHeaderCell: headerCell,
          onCell: bodyCell,
          render: (_, file) => <span className="pkbFileNameCell">
            <FileTextOutlined aria-hidden />
            <button type="button" className="pkbFileLink" aria-label={`预览 ${file.filename}`} onClick={() => onPreview(file)}>{file.filename}</button>
          </span>
        },
        {
          title: "大小",
          dataIndex: "sizeBytes",
          width: 96,
          onHeaderCell: headerCell,
          onCell: bodyCell,
          render: (value: number) => humanSize(value)
        },
        {
          title: "解析状态",
          dataIndex: "status",
          width: 128,
          onHeaderCell: headerCell,
          onCell: bodyCell,
          render: (value: PersonalKnowledgeFileSummary["status"]) => fileStatusTag(value)
        },
        {
          title: "更新人 / 时间",
          width: 190,
          onHeaderCell: headerCell,
          onCell: bodyCell,
          render: (_, file) => <Flex vertical gap={2}>
            <span className="pkbFileUpdater">{file.uploaderName || `#${file.uploaderId}`}</span>
            <span className="pkbFileUpdatedAt">{formatTime(file.updatedAt)}</span>
          </Flex>
        },
        {
          title: "操作",
          key: "actions",
          width: 150,
          onHeaderCell: headerCell,
          onCell: bodyCell,
          render: (_, file) => <PersonalKnowledgeFileRowActions
            file={file}
            canDownload={canDownload}
            canManage={canManage}
            onPreview={onPreview}
            onDownload={onDownload}
            onRename={onRename}
            onDelete={onDelete}
          />
        },
      ]}
    />
  );
}
