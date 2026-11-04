import { ArrowRight } from "lucide-react";
import type { AppCenterListItem } from "../../api/client";

// 空会话首页「看板广场案例参考」：横向滑动卡轨（上图 110px iframe 缩略、下文仅标题，
// 220px 定宽卡禁压缩；2026-11-02-004 由两列左图右文改为上下结构以一屏展示更多案例）。
// iframe 缩略图 src/sandbox/loading/tabIndex/缩放是安全契约，不得改动；
// 卡片不渲染描述，悬停 title 保留「名称+描述」全文；boards 为 null 表示加载中（骨架），
// 空列表由父级隐藏整个区块。

interface Props {
  boards: AppCenterListItem[] | null;
  onOpenBoard: (board: AppCenterListItem) => void;
  onViewAll?: () => void;
}

const skeletonCount = 3;

export function WelcomeBoardGallery({ boards, onOpenBoard, onViewAll }: Props) {
  // 广场无看板时隐藏整个区块（含标题）；加载中（null）显示标题 + 骨架。
  if (boards !== null && boards.length === 0) return null;
  return (
    <section className="chatHomeBoardSection">
      <div className="chatHomeSectionHeader">
        <h3>看板广场案例参考</h3>
        {onViewAll && (
          <button type="button" className="chatHomeViewAll" onClick={onViewAll}>
            查看全部 <ArrowRight size={13} aria-hidden="true" />
          </button>
        )}
      </div>
      <div className="chatHomeBoardRow">
        {boards === null
          ? Array.from({ length: skeletonCount }, (_, index) => (
              <span className="chatHomeBoardCard chatHomeBoardSkeleton" key={index} aria-hidden="true" />
            ))
          : boards.map((board) => (
              <a
                className="chatHomeBoardCard"
                key={board.id}
                href={board.accessUrl}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`打开看板 ${board.name}`}
                title={board.description ? `${board.name}\n${board.description}` : board.name}
                onClick={(event) => {
                  event.preventDefault();
                  onOpenBoard(board);
                }}
              >
                <span className="chatHomeBoardThumb">
                  <iframe
                    src={`/api/v1/app-center/${board.id}/index.html?embed=thumbnail`}
                    title={`${board.name}首页缩略图`}
                    sandbox=""
                    loading="lazy"
                    tabIndex={-1}
                  />
                </span>
                <span className="chatHomeBoardInfo">
                  <strong>{board.name}</strong>
                </span>
              </a>
            ))}
      </div>
    </section>
  );
}
