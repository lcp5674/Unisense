//: API 文档：内嵌 Swagger UI（/docs 由 nginx 反代到 backend），保持系统布局当前窗口展示。
//: 外层固定高度并隐藏滚动，滚动交由 iframe 内部处理，避免双滚动条。
export function ApiDocs() {
  return (
    <div
      style={{
        height: "calc(100vh - 130px)",
        overflow: "hidden",
        borderRadius: 8,
        border: "1px solid rgba(5, 5, 5, 0.06)",
        background: "#fff",
      }}
    >
      <iframe
        src="/docs"
        title="API 文档"
        style={{ width: "100%", height: "100%", border: "none", display: "block" }}
      />
    </div>
  );
}
