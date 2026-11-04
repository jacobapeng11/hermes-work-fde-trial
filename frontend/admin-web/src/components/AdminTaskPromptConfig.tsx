import { useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import type {
  TaskPromptConfigDocument,
  TaskPromptConfigSaveResponse,
  TaskPromptConfigState,
} from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";

type DraftTask = { id: string; summary: string; detail: string };
type DraftTopic = { id: string; title: string; tasks: DraftTask[] };
type DraftTab = {
  id: string;
  label: string;
  description: string;
  isDefault: boolean;
  showBoardGallery: boolean;
  topics: DraftTopic[];
};
type Draft = { revision: number; tabs: DraftTab[] };

let draftSequence = 0;
function nextId(prefix: string) {
  draftSequence += 1;
  return `${prefix}-${draftSequence}`;
}

function withDraftIds(config: TaskPromptConfigDocument | null | undefined, revision: number): Draft {
  return {
    revision,
    tabs: (config?.tabs ?? []).map((tab) => ({
      id: nextId("tab"),
      label: tab.label,
      description: tab.description,
      isDefault: tab.isDefault,
      showBoardGallery: tab.showBoardGallery,
      topics: tab.topics.map((topic) => ({
        id: nextId("topic"),
        title: topic.title,
        tasks: topic.tasks.map((task) => ({
          id: nextId("task"),
          summary: task.summary,
          detail: task.detail,
        })),
      })),
    })),
  };
}

function toDocument(draft: Draft): TaskPromptConfigDocument {
  return {
    tabs: draft.tabs.map((tab) => ({
      label: tab.label.trim(),
      description: tab.description.trim(),
      isDefault: tab.isDefault,
      showBoardGallery: tab.showBoardGallery,
      topics: tab.topics.map((topic) => ({
        title: topic.title.trim(),
        tasks: topic.tasks.map((task) => ({
          summary: task.summary.trim(),
          detail: task.detail.trim(),
        })),
      })),
    })),
  };
}

function move<T>(items: T[], index: number, offset: -1 | 1): T[] {
  const target = index + offset;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function AdminTaskPromptConfig() {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [selectedTabId, setSelectedTabId] = useState<string | null>(null);
  const [selectedTopicId, setSelectedTopicId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const state: TaskPromptConfigState = await adminApi.taskPromptConfig();
      const nextDraft = withDraftIds(state.config, state.revision);
      setDraft(nextDraft);
      setSelectedTabId(nextDraft.tabs[0]?.id ?? null);
      setSelectedTopicId(nextDraft.tabs[0]?.topics[0]?.id ?? null);
    } catch (reason) {
      setError(errorMessage(reason, "任务提示配置加载失败"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const selectedTab = useMemo(
    () => draft?.tabs.find((tab) => tab.id === selectedTabId) ?? draft?.tabs[0] ?? null,
    [draft, selectedTabId]
  );
  const selectedTopic = useMemo(
    () => selectedTab?.topics.find((topic) => topic.id === selectedTopicId)
      ?? selectedTab?.topics[0]
      ?? null,
    [selectedTab, selectedTopicId]
  );
  const dirty = draft !== null && draft.tabs.some((tab) => tab.label.trim() && tab.description.trim());

  function updateTab(tabId: string, changes: Partial<DraftTab>) {
    setDraft((current) => {
      if (!current) return current;
      return {
        ...current,
        tabs: current.tabs.map((tab) => {
          if (tab.id !== tabId) {
            return changes.isDefault ? { ...tab, isDefault: false } : tab;
          }
          return { ...tab, ...changes };
        }),
      };
    });
    setNotice(null);
  }

  function updateTopic(topicId: string, changes: Partial<DraftTopic>) {
    setDraft((current) => {
      if (!current || !selectedTab) return current;
      return {
        ...current,
        tabs: current.tabs.map((tab) => tab.id === selectedTab.id ? {
          ...tab,
          topics: tab.topics.map((topic) => topic.id === topicId ? { ...topic, ...changes } : topic),
        } : tab),
      };
    });
    setNotice(null);
  }

  function updateTask(taskId: string, changes: Partial<DraftTask>) {
    setDraft((current) => {
      if (!current || !selectedTab || !selectedTopic) return current;
      return {
        ...current,
        tabs: current.tabs.map((tab) => tab.id === selectedTab.id ? {
          ...tab,
          topics: tab.topics.map((topic) => topic.id === selectedTopic.id ? {
            ...topic,
            tasks: topic.tasks.map((task) => task.id === taskId ? { ...task, ...changes } : task),
          } : topic),
        } : tab),
      };
    });
    setNotice(null);
  }

  function addTab() {
    if (!draft) return;
    const topic: DraftTopic = {
      id: nextId("topic"),
      title: "新主题",
      tasks: [{ id: nextId("task"), summary: "新典型问题", detail: "请填写详细任务描述。" }],
    };
    const tab: DraftTab = {
      id: nextId("tab"),
      label: "新Tab",
      description: "请填写该Tab的功能描述。",
      isDefault: draft.tabs.length === 0,
      showBoardGallery: false,
      topics: [topic],
    };
    setDraft({ ...draft, tabs: [...draft.tabs, tab] });
    setSelectedTabId(tab.id);
    setSelectedTopicId(topic.id);
    setNotice(null);
  }

  function removeTab(tabId: string) {
    if (!draft) return;
    const tabIndex = draft.tabs.findIndex((tab) => tab.id === tabId);
    if (tabIndex < 0) return;
    const remainingTabs = draft.tabs.filter((tab) => tab.id !== tabId);
    const shouldResetDefault = remainingTabs.length > 0
      && !remainingTabs.some((tab) => tab.isDefault);
    const tabs = remainingTabs.map((tab, index) => ({
      ...tab,
      isDefault: shouldResetDefault && index === 0 ? true : tab.isDefault,
    }));
    setDraft({ ...draft, tabs });
    setSelectedTabId(tabs[tabIndex - 1]?.id ?? tabs[0]?.id ?? null);
    setSelectedTopicId(tabs[tabIndex - 1]?.topics[0]?.id ?? tabs[0]?.topics[0]?.id ?? null);
    setNotice(null);
  }

  function moveTab(tabId: string, offset: -1 | 1) {
    if (!draft) return;
    const index = draft.tabs.findIndex((tab) => tab.id === tabId);
    setDraft({ ...draft, tabs: move(draft.tabs, index, offset) });
    setNotice(null);
  }

  function addTopic() {
    if (!draft || !selectedTab) return;
    const topic: DraftTopic = {
      id: nextId("topic"),
      title: "新主题",
      tasks: [{ id: nextId("task"), summary: "新典型问题", detail: "请填写详细任务描述。" }],
    };
    setDraft({
      ...draft,
      tabs: draft.tabs.map((tab) => tab.id === selectedTab.id ? {
        ...tab,
        topics: [...tab.topics, topic],
      } : tab),
    });
    setSelectedTopicId(topic.id);
    setNotice(null);
  }

  function removeTopic(topicId: string) {
    if (!draft || !selectedTab) return;
    const index = selectedTab.topics.findIndex((topic) => topic.id === topicId);
    const topics = selectedTab.topics.filter((topic) => topic.id !== topicId);
    setDraft({
      ...draft,
      tabs: draft.tabs.map((tab) => tab.id === selectedTab.id ? { ...tab, topics } : tab),
    });
    setSelectedTopicId(topics[index - 1]?.id ?? topics[0]?.id ?? null);
    setNotice(null);
  }

  function moveTopic(topicId: string, offset: -1 | 1) {
    if (!draft || !selectedTab) return;
    const index = selectedTab.topics.findIndex((topic) => topic.id === topicId);
    setDraft({
      ...draft,
      tabs: draft.tabs.map((tab) => tab.id === selectedTab.id ? {
        ...tab,
        topics: move(tab.topics, index, offset),
      } : tab),
    });
    setNotice(null);
  }

  function addTask() {
    if (!draft || !selectedTab || !selectedTopic) return;
    const task: DraftTask = { id: nextId("task"), summary: "新典型问题", detail: "请填写详细任务描述。" };
    setDraft({
      ...draft,
      tabs: draft.tabs.map((tab) => tab.id === selectedTab.id ? {
        ...tab,
        topics: tab.topics.map((topic) => topic.id === selectedTopic.id ? {
          ...topic,
          tasks: [...topic.tasks, task],
        } : topic),
      } : tab),
    });
    setNotice(null);
  }

  function updateTaskList(updater: (tasks: DraftTask[]) => DraftTask[]) {
    if (!draft || !selectedTab || !selectedTopic) return;
    setDraft({
      ...draft,
      tabs: draft.tabs.map((tab) => tab.id === selectedTab.id ? {
        ...tab,
        topics: tab.topics.map((topic) => topic.id === selectedTopic.id ? {
          ...topic,
          tasks: updater(topic.tasks),
        } : topic),
      } : tab),
    });
    setNotice(null);
  }

  async function save() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response: TaskPromptConfigSaveResponse = await adminApi.saveTaskPromptConfig({
        revision: draft.revision,
        config: toDocument(draft),
      });
      const nextDraft = withDraftIds(response.config, response.revision);
      setDraft(nextDraft);
      setSelectedTabId(nextDraft.tabs[0]?.id ?? null);
      setSelectedTopicId(nextDraft.tabs[0]?.topics[0]?.id ?? null);
      setNotice(`已发布任务提示配置 v${response.revision}`);
    } catch (reason) {
      setError(errorMessage(reason, "任务提示配置保存失败"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="adminPanel taskPromptConfigPanel" aria-label="任务提示配置">
      <div className="panelHeader">
        <div>
          <h2>任务提示配置</h2>
          <span>配置个人空任务页的Tab、领域主题和典型问题，保存后立即生效。</span>
        </div>
        <div className="taskPromptActions">
          <strong>{draft ? `v${draft.revision}` : loading ? "…" : "-"}</strong>
          <button type="button" onClick={() => void load()} disabled={loading || saving}><RefreshCw size={15}/>刷新</button>
          <button className="primaryButton" type="button" onClick={() => void save()} disabled={loading || saving || !dirty}>
            <Save size={15}/>{saving ? "正在保存…" : "保存并发布"}
          </button>
        </div>
      </div>

      {error && <div className="adminNotice" role="alert"><strong>操作失败</strong><span>{error}</span></div>}
      {notice && <div className="taskPromptNotice" role="status">{notice}</div>}

      {loading || !draft ? (
        <p className="muted">正在加载任务提示配置…</p>
      ) : (
        <div className="taskPromptConfigLayout">
          <aside className="taskPromptTabList" aria-label="Tab列表">
            {draft.tabs.map((tab, index) => (
              <div className="taskPromptTabListItem" data-active={tab.id === selectedTab?.id} key={tab.id}>
                <button type="button" onClick={() => {
                  setSelectedTabId(tab.id);
                  setSelectedTopicId(tab.topics[0]?.id ?? null);
                }}>
                  <strong>{tab.label || "未命名Tab"}</strong>
                  <small>{tab.isDefault ? "默认" : `${tab.topics.length} 个主题`}</small>
                </button>
                <span>
                  <button type="button" aria-label={`上移 ${tab.label || index + 1}`} disabled={index === 0} onClick={() => moveTab(tab.id, -1)}><ArrowUp size={13}/></button>
                  <button type="button" aria-label={`下移 ${tab.label || index + 1}`} disabled={index === draft.tabs.length - 1} onClick={() => moveTab(tab.id, 1)}><ArrowDown size={13}/></button>
                  <button type="button" aria-label={`删除 ${tab.label || index + 1}`} onClick={() => removeTab(tab.id)}><Trash2 size={13}/></button>
                </span>
              </div>
            ))}
            <button className="taskPromptAddButton" type="button" onClick={addTab}><Plus size={15}/>新增Tab</button>
          </aside>

          {selectedTab ? (
            <div className="taskPromptEditor">
              <div className="taskPromptTabFields">
                <label>Tab名称<input value={selectedTab.label} maxLength={24} onChange={(event) => updateTab(selectedTab.id, { label: event.target.value })}/></label>
                <label className="taskPromptWide">Tab描述<textarea value={selectedTab.description} maxLength={200} rows={3} onChange={(event) => updateTab(selectedTab.id, { description: event.target.value })}/></label>
                <label className="taskPromptCheckbox"><input type="checkbox" checked={selectedTab.isDefault} onChange={(event) => updateTab(selectedTab.id, { isDefault: event.target.checked })}/>默认Tab</label>
                <label className="taskPromptCheckbox"><input type="checkbox" checked={selectedTab.showBoardGallery} onChange={(event) => updateTab(selectedTab.id, { showBoardGallery: event.target.checked })}/>显示看板广场缩略图</label>
              </div>

              <div className="taskPromptSectionHeader">
                <h3>主题</h3>
                <button type="button" onClick={addTopic}><Plus size={15}/>新增主题</button>
              </div>
              <div className="taskPromptTopicList" role="tablist" aria-label="主题列表">
                {selectedTab.topics.map((topic, index) => (
                  <div className="taskPromptTopicItem" key={topic.id}>
                    <button type="button" role="tab" aria-selected={topic.id === selectedTopic?.id} onClick={() => setSelectedTopicId(topic.id)}>{topic.title || "未命名主题"}</button>
                    <span>
                      <button type="button" aria-label={`上移主题 ${topic.title || index + 1}`} disabled={index === 0} onClick={() => moveTopic(topic.id, -1)}><ArrowUp size={13}/></button>
                      <button type="button" aria-label={`下移主题 ${topic.title || index + 1}`} disabled={index === selectedTab.topics.length - 1} onClick={() => moveTopic(topic.id, 1)}><ArrowDown size={13}/></button>
                      <button type="button" aria-label={`删除主题 ${topic.title || index + 1}`} onClick={() => removeTopic(topic.id)}><Trash2 size={13}/></button>
                    </span>
                  </div>
                ))}
              </div>

              {selectedTopic && (
                <>
                  <div className="taskPromptSectionHeader">
                    <h3>主题与典型问题</h3>
                    <button type="button" onClick={addTask}><Plus size={15}/>新增典型问题</button>
                  </div>
                  <label className="taskPromptTopicTitle">主题标题<input value={selectedTopic.title} maxLength={32} onChange={(event) => updateTopic(selectedTopic.id, { title: event.target.value })}/></label>
                  <div className="taskPromptTaskList">
                    {selectedTopic.tasks.map((task, index) => (
                      <div className="taskPromptTaskItem" key={task.id}>
                        <div className="taskPromptTaskItemHeader">
                          <strong>典型问题 {index + 1}</strong>
                          <span>
                            <button type="button" aria-label={`上移典型问题 ${index + 1}`} disabled={index === 0} onClick={() => updateTaskList((tasks) => move(tasks, index, -1))}><ArrowUp size={13}/></button>
                            <button type="button" aria-label={`下移典型问题 ${index + 1}`} disabled={index === selectedTopic.tasks.length - 1} onClick={() => updateTaskList((tasks) => move(tasks, index, 1))}><ArrowDown size={13}/></button>
                            <button type="button" aria-label={`删除典型问题 ${index + 1}`} onClick={() => updateTaskList((tasks) => tasks.filter((item) => item.id !== task.id))}><Trash2 size={13}/></button>
                          </span>
                        </div>
                        <label>简单描述<input value={task.summary} maxLength={80} onChange={(event) => updateTask(task.id, { summary: event.target.value })}/></label>
                        <label className="taskPromptWide">详细描述<textarea value={task.detail} maxLength={4000} rows={4} onChange={(event) => updateTask(task.id, { detail: event.target.value })}/></label>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="taskPromptEmpty">尚未配置Tab。请先新增Tab。</div>
          )}

        </div>
      )}
    </section>
  );
}
