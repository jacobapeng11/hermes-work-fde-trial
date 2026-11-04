import { Button, theme } from "antd";
import { AppstoreOutlined, UploadOutlined } from "@ant-design/icons";

export interface SkillPageHeaderProps {
  title: string;
  subtitle: string;
  uploadAllowed: boolean;
  uploadLabel: string;
  onUploadOpen: () => void;
}

// 页头（设计 §2）：图标容器（主色浅底）+ 标题 + 副标题 + 右侧主操作。
// 上传按钮从工具栏上移至此（2026-10-24 视觉优化，文案与行为不变）。
export function SkillPageHeader({ title, subtitle, uploadAllowed, uploadLabel, onUploadOpen }: SkillPageHeaderProps) {
  const { token } = theme.useToken();
  return (
    <header className="skillPageHeader">
      <div className="skillPageHeaderMain">
        <span
          className="skillPageHeaderIcon"
          style={{ background: token.colorPrimaryBg, color: token.colorPrimary }}
        ><AppstoreOutlined aria-hidden /></span>
        <div className="skillPageHeaderText">
          <h2 className="skillPageHeaderTitle">{title}</h2>
          <p className="skillPageHeaderSubtitle">{subtitle}</p>
        </div>
      </div>
      {uploadAllowed && (
        <Button
          type="primary"
          autoInsertSpace={false}
          icon={<UploadOutlined aria-hidden />}
          onClick={onUploadOpen}
        >{uploadLabel}</Button>
      )}
    </header>
  );
}
