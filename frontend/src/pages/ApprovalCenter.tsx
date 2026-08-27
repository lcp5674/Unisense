import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Empty, Tabs } from "antd";
import { MetricReview } from "./MetricReview";
import { MasterDataReview } from "./MasterDataReview";
import { ReviewWorkbench } from "./ReviewWorkbench";
import { usePermission } from "../hooks/usePermission";

type ApprovalTab = "metrics" | "master-data" | "conflict";

const TAB_ORDER: ApprovalTab[] = ["metrics", "master-data", "conflict"];

function isApprovalTab(v: string | null): v is ApprovalTab {
  return v === "metrics" || v === "master-data" || v === "conflict";
}

/** 统一审批中心（TD §13）：指标审批 / 主数据审批 / 冲突仲裁 三合一。
 *  三个工作台以 embedded 模式内嵌（隐藏各自 page-head），经 URL ?tab= 深链直达
 *  （原 /metrics/review、/master-data/review、/review 深链路由重定向到对应 Tab）。
 *  权限语义由内部 tab 可见性控制，与原三个入口各自的权限点判定一致。 */
export function ApprovalCenter() {
  const [params, setParams] = useSearchParams();
  const { can } = usePermission();

  const active = isApprovalTab(params.get("tab")) ? (params.get("tab") as ApprovalTab) : "metrics";

  // 按权限点过滤可见 tab：指标 metric:review / 主数据 master-data:review / 冲突 review:view
  const tabs = useMemo(
    () =>
      TAB_ORDER.filter((t) => {
        if (t === "metrics") return can("metric:review");
        if (t === "master-data") return can("master-data:review");
        return can("review:view");
      }),
    [can],
  );

  // 当前激活 tab 无权限时回落到第一个可见 tab（深链到无权 tab 不白屏）
  const safeActive = tabs.includes(active) ? active : (tabs[0] ?? "metrics");

  function handleChange(key: string) {
    setParams({ tab: key }, { replace: true });
  }

  if (tabs.length === 0) {
    return <Empty description="您没有审批 / 仲裁权限" style={{ marginTop: 64 }} />;
  }

  return (
    <Tabs
      activeKey={safeActive}
      onChange={handleChange}
      items={[
        ...(tabs.includes("metrics")
          ? [{ key: "metrics" as ApprovalTab, label: "指标审批", children: <MetricReview embedded /> }]
          : []),
        ...(tabs.includes("master-data")
          ? [
              {
                key: "master-data" as ApprovalTab,
                label: "主数据审批",
                children: <MasterDataReview embedded />,
              },
            ]
          : []),
        ...(tabs.includes("conflict")
          ? [{ key: "conflict" as ApprovalTab, label: "冲突仲裁", children: <ReviewWorkbench embedded /> }]
          : []),
      ]}
    />
  );
}
