import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function EvidenceMarkdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        table: ({ children }) => (
          <div className="evidence-table-scroll" role="region" aria-label="原文数据表，可横向滚动" tabIndex={0}>
            <table>{children}</table>
          </div>
        ),
        img: ({ alt }) => <span className="evidence-image-notice">图片链接未自动加载{alt ? `：${alt}` : ""}</span>,
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
