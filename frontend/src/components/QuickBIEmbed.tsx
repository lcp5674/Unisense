import { useEffect, useState } from "react";
import { Spin, Alert } from "antd";
import { fetchQuickBITicket } from "../api";

interface QuickBIEmbedProps {
  reportId: string;
  dashboardId?: string;
  params?: Record<string, string>;
}

export function QuickBIEmbed({ reportId, dashboardId, params }: QuickBIEmbedProps) {
  const [iframeUrl, setIframeUrl] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadTicket() {
      setLoading(true);
      setError(null);
      try {
        const data = await fetchQuickBITicket({ reportId, dashboardId, params });
        if (cancelled) return;

        // Build iframe URL with ticket
        const url = new URL(data.embed_url || "https://quickbi.aliyun.com/embed");
        url.searchParams.set("ticket", data.ticket);
        url.searchParams.set("reportId", reportId);
        if (dashboardId) {
          url.searchParams.set("dashboardId", dashboardId);
        }
        if (params) {
          Object.entries(params).forEach(([k, v]) => {
            url.searchParams.set(k, v);
          });
        }
        setIframeUrl(url.toString());
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "QuickBI 嵌入加载失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    loadTicket();
    return () => {
      cancelled = true;
    };
  }, [reportId, dashboardId, params]);

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 48 }}>
        <Spin size="large" tip="加载 QuickBI 报表中..." />
      </div>
    );
  }

  if (error) {
    return (
      <Alert
        type="error"
        message="QuickBI 嵌入失败"
        description={error}
        showIcon
      />
    );
  }

  return (
    <iframe
      src={iframeUrl}
      title="QuickBI Report"
      style={{
        width: "100%",
        height: 600,
        border: "none",
        borderRadius: 8,
      }}
      allowFullScreen
    />
  );
}
