import { useEffect, useState } from "react";
import { Alert, Button, Divider, Input, Modal, Select, Upload } from "antd";
import { LoadingOutlined, UploadOutlined } from "@ant-design/icons";
import type { SkillCategorySummary } from "@hermes-cloud/schemas";
import { errorMessage } from "./shared";
import { usePlatformTheme } from "../../theme/platform-theme";

export interface SkillUploadDialogProps {
  open: boolean;
  title: string;
  description: string;
  dialogLabel: string;
  categories: SkillCategorySummary[];
  allowCreateCategory: boolean;
  onSubmit: (file: File, categoryKey: string, description?: string) => Promise<void>;
  onCreateCategory: (displayName: string) => Promise<SkillCategorySummary>;
  onClose: () => void;
}

// 弹窗常挂载走 open 属性（不条件卸载）：分类选择跨开关保留，
// 与迁移前根组件持有状态时的存续语义一致；关闭时仅重置临时字段。
// dialogLabel 保留在 props 中以维持调用方契约；antd Modal 的可访问名由 title 提供。
export function SkillUploadDialog({
  open,
  title,
  description,
  dialogLabel: _dialogLabel,
  categories,
  allowCreateCategory,
  onSubmit,
  onCreateCategory,
  onClose,
}: SkillUploadDialogProps) {
  const { theme } = usePlatformTheme();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [categoryKey, setCategoryKey] = useState<string | undefined>(undefined);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadDescriptionText, setUploadDescriptionText] = useState("");
  const [creatingCategory, setCreatingCategory] = useState(false);
  const [searchValue, setSearchValue] = useState("");

  useEffect(() => {
    if (open) return;
    setSelectedFile(null);
    setUploadError(null);
    setUploadDescriptionText("");
  }, [open]);

  const selectUploadFile = (file: File | null) => {
    if (!file) {
      setSelectedFile(null);
      setUploadError(null);
      return;
    }
    if (!file.name.toLocaleLowerCase().endsWith(".zip")) {
      setSelectedFile(null);
      setUploadError("请选择 .zip 格式的技能包。");
      return;
    }
    setSelectedFile(file);
    setUploadError(null);
  };

  const closeUpload = () => {
    if (uploading) return;
    setSelectedFile(null);
    setUploadError(null);
    setUploadDescriptionText("");
    onClose();
  };

  const submitUpload = async () => {
    if (!selectedFile || uploading) return;
    setUploading(true);
    setUploadError(null);
    try {
      const descriptionText = uploadDescriptionText.trim();
      if (descriptionText) {
        await onSubmit(selectedFile, categoryKey || "uncategorized", descriptionText);
      } else {
        await onSubmit(selectedFile, categoryKey || "uncategorized");
      }
    } catch (error: unknown) {
      setUploadError(errorMessage(error, "技能包上传失败，请检查文件后重试。"));
    } finally {
      setUploading(false);
    }
  };

  const createCategory = async () => {
    const displayName = searchValue.trim();
    if (!displayName || creatingCategory || uploading) return;
    setCreatingCategory(true);
    setUploadError(null);
    try {
      const created = await onCreateCategory(displayName);
      setCategoryKey(created.categoryKey);
      setSearchValue("");
    } catch (error: unknown) {
      setUploadError(errorMessage(error, "分类创建失败，请重试。"));
    } finally {
      setCreatingCategory(false);
    }
  };

  return (
    <Modal
      open={open}
      title={title}
      centered
      width={520}
      rootClassName={theme === "dark" ? "skillDialogDark" : ""}
      className="skillUploadDialog"
      closable={!uploading}
      maskClosable={!uploading}
      keyboard={!uploading}
      onCancel={closeUpload}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={uploading} onClick={closeUpload}>取消</Button>,
        <Button
          key="submit"
          type="primary"
          autoInsertSpace={false}
          disabled={!selectedFile}
          loading={uploading}
          onClick={() => void submitUpload()}
        >开始上传</Button>,
      ]}
    >
      <div className="skillUploadForm">
        <div className="skillUploadFormItem">
          <label className="skillUploadFormLabel">技能包</label>
          <Upload.Dragger
            accept=".zip,application/zip"
            maxCount={1}
            showUploadList={false}
            aria-label="选择技能 zip 包"
            beforeUpload={(file) => {
              selectUploadFile(file);
              return false;
            }}
          >
            <div className="skillUploadDropInner">
              <UploadOutlined style={{ fontSize: 28 }} aria-hidden />
              <strong>{selectedFile?.name ?? "选择技能 zip 包"}</strong>
              <span>{selectedFile ? `${Math.ceil(selectedFile.size / 1024)} KB · 等待上传` : "支持 .zip / application/zip"}</span>
            </div>
          </Upload.Dragger>
          {selectedFile && <p className="skillUploadFileState">已选择：{selectedFile.name}</p>}
        </div>
        <div className="skillUploadFormItem">
          <label className="skillUploadFormLabel">技能描述（可选）</label>
          <Input.TextArea
            aria-label="技能描述"
            value={uploadDescriptionText}
            maxLength={2000}
            rows={4}
            showCount
            placeholder="不填写则使用技能包内默认描述"
            onChange={(event) => setUploadDescriptionText(event.target.value)}
          />
        </div>
        <div className="skillUploadFormItem">
          <label className="skillUploadFormLabel">技能分类</label>
          <Select
            popupClassName="skillSelectDropdown"
            aria-label="技能分类"
            value={categoryKey}
            onChange={setCategoryKey}
            style={{ width: "100%" }}
            placeholder="搜索或创建新分类"
            allowClear
            showSearch
            searchValue={searchValue}
            onSearch={setSearchValue}
            filterOption={(input, option) =>
              (option?.label ?? "").toLowerCase().includes(input.toLowerCase())
            }
            optionFilterProp="label"
            options={categories
              .filter((item) => item.categoryKey !== "system" && item.isPersonalVisible !== false)
              .map((item) => ({ value: item.categoryKey, label: item.displayName }))}
            dropdownRender={(menu) => (
              <>
                {menu}
                {allowCreateCategory && searchValue.trim() && (
                  <>
                    <Divider style={{ margin: "4px 0" }} />
                    <div
                      role="option"
                      aria-selected={false}
                      style={{ padding: "5px 12px", cursor: "pointer", color: "#2563eb", fontSize: 14 }}
                      onMouseDown={(e) => { e.preventDefault(); void createCategory(); }}
                      onMouseEnter={(e) => (e.currentTarget as HTMLElement).style.background = "#f5f5f5"}
                      onMouseLeave={(e) => (e.currentTarget as HTMLElement).style.background = ""}
                    >
                      ＋ 新建 &ldquo;{searchValue.trim()}&rdquo;
                    </div>
                  </>
                )}
              </>
            )}
          />
        </div>
      </div>
      {uploadError && <Alert className="skillUploadError" type="error" showIcon message={uploadError} />}
    </Modal>
  );
}
