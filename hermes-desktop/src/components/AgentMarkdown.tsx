import { isValidElement, memo, useState, type ComponentProps, type ReactElement, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type CodeElementProps = {
  children?: ReactNode;
  className?: string;
};

function isExternalLink(href: string | undefined): boolean {
  return href !== undefined && /^(?:https?:)?\/\//i.test(href);
}

function CollapsibleCode({ children }: { children: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const codeElement = isValidElement(children)
    ? (children as ReactElement<CodeElementProps>)
    : null;
  const text = String(codeElement?.props.children ?? "").replace(/\n$/, "");
  const className = codeElement?.props.className;
  const language = /(?:^|\s)language-([^\s]+)/.exec(className ?? "")?.[1];
  const collapsible = text.split("\n").length > 15 || text.length > 800;
  const collapsed = collapsible && !expanded;

  return (
    <div
      className="markdownCodeBlock"
      data-collapsed={collapsed ? "true" : "false"}
      data-testid="markdown-code-block"
    >
      {language ? <span className="markdownCodeLanguage">{language}</span> : null}
      <pre>
        <code className={className}>{text}</code>
      </pre>
      {collapsible ? (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起代码" : "展开代码"}
        </button>
      ) : null}
    </div>
  );
}

const agentMarkdownComponents: NonNullable<ComponentProps<typeof ReactMarkdown>["components"]> = {
  a: ({ href, children }) => {
    const external = isExternalLink(href);
    return (
      <a
        href={href}
        rel={external ? "noreferrer noopener" : undefined}
        target={external ? "_blank" : undefined}
      >
        {children}
      </a>
    );
  },
  table: ({ children }) => (
    <div className="markdownTableScroller">
      <table>{children}</table>
    </div>
  ),
  pre: ({ children }) => <CollapsibleCode>{children}</CollapsibleCode>,
};

export const AgentMarkdownContent = memo(function AgentMarkdownContent({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={agentMarkdownComponents}>
      {content}
    </ReactMarkdown>
  );
});

export const AgentMarkdown = memo(function AgentMarkdown({ content }: { content: string }) {
  return <div className="agentMarkdown"><AgentMarkdownContent content={content}/></div>;
});
