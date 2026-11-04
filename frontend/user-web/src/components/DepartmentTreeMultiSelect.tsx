import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DepartmentTreeNode } from "@hermes-cloud/schemas";
import { ChevronDown, ChevronRight } from "lucide-react";


interface Props {
  value: string[];
  onChange: (value: string[]) => void;
  tree: DepartmentTreeNode[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
}


function flatten(nodes: DepartmentTreeNode[], result: Map<string, DepartmentTreeNode> = new Map()) {
  nodes.forEach((node) => {
    result.set(node.departmentId, node);
    flatten(node.children, result);
  });
  return result;
}


function filterTree(nodes: DepartmentTreeNode[], query: string): DepartmentTreeNode[] {
  if (!query) return nodes;
  return nodes.flatMap((node) => {
    const children = filterTree(node.children, query);
    return node.name.toLowerCase().includes(query) || children.length
      ? [{ ...node, children }]
      : [];
  });
}


export function DepartmentTreeMultiSelect({ value, onChange, tree, loading = false, error = null, onRetry }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const byId = useMemo(() => flatten(tree), [tree]);
  const visibleTree = useMemo(() => filterTree(tree, query.trim().toLowerCase()), [tree, query]);

  const closePicker = useCallback(() => {
    setOpen(false);
    setQuery("");
  }, []);
  useEffect(() => {
    const handleOutsidePointerDown = (event: PointerEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      closePicker();
    };
    document.addEventListener("pointerdown", handleOutsidePointerDown);
    return () => document.removeEventListener("pointerdown", handleOutsidePointerDown);
  }, [closePicker]);

  const toggle = (id: string) => {
    onChange(value.includes(id) ? value.filter((item) => item !== id) : [...value, id]);
    closePicker();
  };
  const toggleExpanded = (id: string) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const renderNodes = (nodes: DepartmentTreeNode[], depth = 0): JSX.Element[] => nodes.map((node) => {
    const hasChildren = node.children.length > 0;
    const showChildren = Boolean(query.trim()) || expanded.has(node.departmentId);
    return <li key={node.departmentId}>
      <div style={{ paddingLeft: `${depth * 18}px` }}>
        {hasChildren ? <button type="button" className="departmentTreeToggle" aria-label={`${showChildren ? "收起" : "展开"} ${node.name}`} onClick={() => toggleExpanded(node.departmentId)}>{showChildren ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button> : <span className="departmentTreeSpacer"/>}
        <label><input type="checkbox" aria-label={node.name} checked={value.includes(node.departmentId)} onChange={() => toggle(node.departmentId)}/><span>{node.name}</span></label>
      </div>
      {hasChildren && showChildren && <ul>{renderNodes(node.children, depth + 1)}</ul>}
    </li>;
  });

  return <div className="departmentTreeSelect" ref={rootRef}>
    <div className="directoryMultiSelectTags">
      {value.map((id) => {
        const label = byId.get(id)?.name || `部门 ${id}`;
        return <span key={id}>{label}<button type="button" aria-label={`移除 ${label}`} onClick={() => onChange(value.filter((item) => item !== id))}>×</button></span>;
      })}
      <button type="button" className="departmentTreeOpen" aria-label="选择部门" onClick={() => setOpen((current) => !current)}>选择部门</button>
    </div>
    {open && <div className="departmentTreePopup">
      <input type="search" aria-label="搜索部门" placeholder="搜索部门" value={query} onChange={(event) => setQuery(event.target.value)}/>
      {loading && <p>正在加载部门…</p>}
      {error && <div><p className="pkbError">{error}</p>{onRetry && <button type="button" onClick={onRetry}>重试</button>}</div>}
      {!loading && !error && visibleTree.length === 0 && <p>未找到匹配部门</p>}
      {!loading && !error && <ul className="departmentTreeNodes">{renderNodes(visibleTree)}</ul>}
    </div>}
  </div>;
}
