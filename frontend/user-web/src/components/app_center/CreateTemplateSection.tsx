import type { AppCenterListItem } from "../../api/client";

// 新建看板弹窗「创建方式二：模板库」（2026-11-02-005）：展示真实广场看板
// （AppCenterPage 下传 plaza 派生列表，与首页案例广场同源同形态：横向滑动卡轨 +
// 110px iframe 缩略 + 标题单行截断），选中后由弹窗底部「生成看板」走广场模板
// ZIP 附件注入链路（onCreateFromTemplate）；轨道末尾保留「从空白开始」虚线卡
// （空 prompt 新会话）。原 2026-11-02-001 内置预设模板字典已随本变更退役。

export interface TemplateSelection {
  id: string;
  prompt: string;
  blank: boolean;
  app?: AppCenterListItem;
}

interface Props {
  boards: AppCenterListItem[];
  loading: boolean;
  selectedTemplateId: string | null;
  disabled: boolean;
  onSelectTemplate: (template: TemplateSelection) => void;
}

const skeletonCount = 3;

export function CreateTemplateSection({ boards, loading, selectedTemplateId, disabled, onSelectTemplate }: Props) {
  return (
    <section className="appCenterCreateSection">
      <h3 className="appCenterCreateCardTitle appCenterCreateCardTitleMuted">创建方式二：模板库</h3>
      {loading ? (
        <div className="appCenterCreateBoardRow" aria-busy="true">
          {Array.from({ length: skeletonCount }, (_, index) => (
            <span className="appCenterCreateBoardCard appCenterCreateBoardSkeleton" key={index} aria-hidden="true" />
          ))}
        </div>
      ) : (
        <>
          <div className="appCenterCreateBoardRow">
            {boards.map((board) => (
              <button
                type="button"
                key={board.id}
                aria-pressed={selectedTemplateId === `board-${board.id}`}
                aria-label={`使用广场模板 ${board.name}`}
                title={board.description ? `${board.name}\n${board.description}` : board.name}
                className={selectedTemplateId === `board-${board.id}`
                  ? "appCenterCreateBoardCard is-active"
                  : "appCenterCreateBoardCard"}
                disabled={disabled}
                onClick={() => onSelectTemplate({
                  id: `board-${board.id}`,
                  prompt: "",
                  blank: false,
                  app: board
                })}
              >
                <span className="appCenterCreateBoardThumb">
                  <iframe
                    src={`/api/v1/app-center/${board.id}/index.html?embed=thumbnail`}
                    title={`${board.name}首页缩略图`}
                    sandbox=""
                    loading="lazy"
                    tabIndex={-1}
                  />
                </span>
                <span className="appCenterCreateBoardName">{board.name}</span>
              </button>
            ))}
            <button
              type="button"
              aria-pressed={selectedTemplateId === "blank"}
              aria-label="从空白开始"
              className={selectedTemplateId === "blank"
                ? "appCenterCreateBoardCard appCenterCreateBlankCard is-active"
                : "appCenterCreateBoardCard appCenterCreateBlankCard"}
              disabled={disabled}
              onClick={() => onSelectTemplate({ id: "blank", prompt: "", blank: true })}
            >
              <span className="appCenterCreateBoardThumb appCenterCreateBlankCover">
                <svg
                  className="appCenterCreateBlankIcon"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                  aria-hidden="true"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 4v16m8-8H4" />
                </svg>
              </span>
              <span className="appCenterCreateBoardName">从空白开始</span>
            </button>
          </div>
          {boards.length === 0 && (
            <p className="appCenterCreateBoardEmpty">广场暂无共享看板，可直接从空白开始，或用方式一的提示词生成。</p>
          )}
        </>
      )}
    </section>
  );
}
