import { useEffect, useState } from "react";

export function useExpertEditorAnchors(prefix: "create" | "edit", open: boolean) {
  const [active, setActive] = useState("basic");

  useEffect(() => {
    if (!open || typeof IntersectionObserver === "undefined") return;
    const sections = ["basic", "visibility", "skill", "mcp", "knowledge"]
      .map((id) => document.getElementById(`${prefix}-${id}`))
      .filter((node): node is HTMLElement => Boolean(node));
    const root = sections[0]?.closest(prefix === "create" ? ".expertDialogBody" : ".expertEditorBody");
    if (!root) return;
    const atBottom = () => root.scrollTop + root.clientHeight >= root.scrollHeight - 2;
    const activateLastAtBottom = () => {
      const last = sections[sections.length - 1];
      if (atBottom()) setActive(last?.id.replace(`${prefix}-`, "") || "basic");
    };
    const observer = new IntersectionObserver((entries) => {
      if (atBottom()) {
        activateLastAtBottom();
        return;
      }
      const visible = entries.filter((entry) => entry.isIntersecting)
        .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top)[0];
      if (visible) setActive(visible.target.id.replace(`${prefix}-`, ""));
    }, { root, rootMargin: "-12% 0px -70%", threshold: [0, 0.1] });
    sections.forEach((section) => observer.observe(section));
    root.addEventListener("scroll", activateLastAtBottom, { passive: true });
    return () => {
      observer.disconnect();
      root.removeEventListener("scroll", activateLastAtBottom);
    };
  }, [open, prefix]);

  const select = (id: string) => {
    setActive(id);
    document.getElementById(`${prefix}-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return { active, select };
}
