import { Button, Card, Tag } from "antd";
import { DeleteOutlined, LoadingOutlined } from "@ant-design/icons";
import type {
  PendingSkillOperation,
  ProfileSkillCatalogItem,
  ProfileSkillConfiguredItem,
  SkillCategorySummary,
} from "@hermes-cloud/schemas";
import type { MineSkillViewItem, PersonalSkillViewItem } from "../../state/plaza-skill-store";
import type { RemovalConfirmation, SkillSelection } from "./PlazaSkillPanel";
import { activeOperation, categoryLabel, operationLabel, versionLabel } from "./shared";

export interface SkillCardProps {
  kind: "market" | "mine";
  categories: SkillCategorySummary[];
  item: ProfileSkillCatalogItem | MineSkillViewItem | ProfileSkillConfiguredItem;
  operation?: PendingSkillOperation;
  onOpen: (selection: SkillSelection) => void;
  onRun: (selection: SkillSelection) => void;
  onRemovalRequest: (removal: RemovalConfirmation) => void;
}



const cardBodyStyle = {
  padding: 16,
  height: "100%",
  display: "flex",
  flexDirection: "column",
} as const;

export function SkillCard({ kind, categories, item, operation, onOpen, onRun, onRemovalRequest }: SkillCardProps) {
  if (kind === "market") {
    const marketItem = item as ProfileSkillCatalogItem;
    const busy = activeOperation(operation) || marketItem.operating;
    const installed = marketItem.state !== "installable";
    return (
      <Card
        className="skillCatalogCard"
        hoverable
        styles={{ body: cardBodyStyle }}
        classNames={{ body: "skillCatalogCardBody" }}
        tabIndex={0}
        aria-label={`查看 ${marketItem.name} 详情`}
        onClick={() => onOpen({ kind: "market", item: marketItem })}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") onOpen({ kind: "market", item: marketItem });
        }}
      >
        <h3>{marketItem.name}</h3>
        <p title={marketItem.description || undefined}>{marketItem.description || "平台公共技能，安装后即可在当前 Hermes 中使用。"}</p>
        <div className="skillCardTags">
          <Tag>{versionLabel(marketItem.currentVersionLabel)}</Tag>
          <Tag>{categoryLabel(marketItem.categoryKey, categories)}</Tag>
        </div>
        <footer>
          <Button
            size="small"
            autoInsertSpace={false}
            color={installed ? "danger" : "primary"}
            variant="outlined"
                        disabled={busy || marketItem.state === "update_available" || (marketItem.executionStatus === "force_disabled" && !installed)}
            icon={busy ? <LoadingOutlined spin aria-hidden /> : undefined}
            onClick={(event) => {
              event.stopPropagation();
              if (installed) onRemovalRequest({ kind: "market", item: marketItem });
              else void onRun({ kind: "market", item: marketItem });
            }}
          >
            {busy ? operationLabel(operation, "处理中") : installed ? "卸载" : "安装"}
          </Button>
        </footer>
      </Card>
    );
  }

  const mineItem = item as MineSkillViewItem | ProfileSkillConfiguredItem;
  const autoUpdating = mineItem.source === "market" && "updateStatus" in mineItem && mineItem.updateStatus === "update_available";
  const busy = activeOperation(operation) || mineItem.operating || autoUpdating;
  const hermesManaged = mineItem.source === "hermes";
  const personal = (mineItem.source === "personal" || hermesManaged) && "desired" in mineItem;
  const system = mineItem.source === "system";
  const applicationPrivate = mineItem.source === "application" && mineItem.catalogAssetId == null;
  const archived = personal && (mineItem as PersonalSkillViewItem).status === "archived";
  const installed = personal ? (mineItem as PersonalSkillViewItem).desired : true;
  return (
    <Card
      className="skillCatalogCard"
      hoverable
      styles={{ body: cardBodyStyle }}
        classNames={{ body: "skillCatalogCardBody" }}
      tabIndex={0}
      aria-label={`查看 ${mineItem.name} 详情`}
      onClick={() => onOpen({ kind: "mine", item: mineItem as ProfileSkillConfiguredItem })}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onOpen({ kind: "mine", item: mineItem as ProfileSkillConfiguredItem });
      }}
    >
      {personal && !hermesManaged && (
        <button
          type="button"
          className="skillCatalogTrash"
          aria-label={`删除 ${mineItem.name}`}
          disabled={busy}
          onClick={(event) => {
            event.stopPropagation();
            onRemovalRequest({ kind: "personal_delete", item: mineItem as PersonalSkillViewItem });
          }}
        ><DeleteOutlined style={{ fontSize: 15, color: "#ff5722" }} /></button>
      )}
      <h3>{mineItem.name}</h3>
      <p title={mineItem.description || undefined}>
        {mineItem.description || (system ? "暂无技能说明。" : mineItem.source === "application" ? "当前应用 Agent 独有的技能。" : "个人上传的技能能力。")}
      </p>
      <div className="skillCardTags">
        <Tag>{versionLabel(mineItem.versionLabel)}</Tag>
        <Tag>{categoryLabel(mineItem.categoryKey, categories)}</Tag>
      </div>
      {archived && <p className="skillCatalogStatus">已归档，请重新上传同名 Skill 恢复。</p>}
      {!system && !archived && !hermesManaged && <footer>
        <Button
          size="small"
          autoInsertSpace={false}
          color={installed ? "danger" : "primary"}
            variant="outlined"
                    disabled={busy}
          icon={busy ? <LoadingOutlined spin aria-hidden /> : undefined}
          onClick={(event) => {
            event.stopPropagation();
            if (!installed) void onRun({ kind: "mine", item: mineItem as ProfileSkillConfiguredItem });
            else if (personal) onRemovalRequest({ kind: "personal_uninstall", item: mineItem as PersonalSkillViewItem });
            else if (applicationPrivate) onRemovalRequest({ kind: "application_delete", item: mineItem as ProfileSkillConfiguredItem });
            else onRemovalRequest({ kind: "configured", item: mineItem as ProfileSkillConfiguredItem });
          }}
        >
          {autoUpdating ? "自动更新中" : busy ? operationLabel(operation, "处理中") : installed ? applicationPrivate ? "删除" : "卸载" : "安装"}
        </Button>
      </footer>}
    </Card>
  );
}
