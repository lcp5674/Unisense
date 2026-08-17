import { useEffect, useState } from "react";
import { Alert, Modal, Spin } from "antd";
import { compareMetricsMatrix } from "../api";
import type { MetricCompareMatrixResult } from "../types";
import { MetricCompareMatrixTable } from "./MetricCompareMatrixTable";

const MIN_COMPARE = 2;
const MAX_COMPARE = 6;

/**
 * 指标矩阵对比弹窗（指标目录「对比所选」使用）：勾选 2~6 个指标后在当前页内直接
 * 以弹窗展示对比矩阵，无需跳转对比页，减少用户来回切换。
 * 纯展示逻辑复用 MetricCompareMatrixTable；请求、加载/错误态由本组件自管。
 */
export function MetricCompareModal({
  open,
  codes,
  onClose,
}: {
  open: boolean;
  codes: string[];
  onClose: () => void;
}) {
  const [result, setResult] = useState<MetricCompareMatrixResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // open/codes 任一变化都重置并重新对比；关闭或不满足 2~6 时清空
  useEffect(() => {
    if (!open || codes.length < MIN_COMPARE || codes.length > MAX_COMPARE) {
      setResult(null);
      setError(null);
      return undefined;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    setResult(null);
    compareMetricsMatrix(codes)
      .then((res) => {
        if (alive) setResult(res);
      })
      .catch((err) => {
        if (alive) setError(err instanceof Error ? err.message : "对比失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, codes]);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title={`指标对比 (${codes.length})`}
      width={Math.min(1240, 260 + 110 + codes.length * 260)}
      footer={null}
      destroyOnHidden
      style={{ top: 32 }}
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : error ? (
        <Alert type="error" showIcon message={error} />
      ) : result ? (
        <MetricCompareMatrixTable result={result} />
      ) : (
        <Alert type="info" showIcon message="请勾选 2~6 个指标后进行对比" />
      )}
    </Modal>
  );
}
