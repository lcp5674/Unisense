/**
 * 指标编码校验（与后端 ConflictPrechecker.validate_code_format 保持一致）：
 * 4 段式「域_业务对象_度量_统计周期」，每段小写字母开头 + 小写字母数字下划线。
 * 留空返回 null（由后端自动生成），显式值非 4 段或段格式非法返回错误信息。
 *
 * 注意：不能依赖整串正则判定 4 段——`[a-z0-9_]*` 会吞段间下划线（如 a_b_c_d_e
 * 5 段也能被整串正则匹配），必须与后端一致用 split("_") 数段再逐段校验。
 */

const CODE_SEGMENT_PATTERN = /^[a-z][a-z0-9_]*$/;

// 供测试/展示参考（非 4 段判定依据，见上方注释）
export const METRIC_CODE_PATTERN =
  /^[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*$/;

const SEGMENT_LABELS = ["域", "业务对象", "度量", "统计周期"];

export function validateMetricCode(code: string | undefined | null): string | null {
  if (!code || !code.trim()) return null; // 留空 → 系统自动生成
  const c = code.trim();
  const parts = c.split("_");
  if (parts.length !== 4) {
    return `须符合 4 段格式（域_业务对象_度量_统计周期），当前 ${parts.length} 段`;
  }
  for (let i = 0; i < parts.length; i += 1) {
    if (!CODE_SEGMENT_PATTERN.test(parts[i])) {
      return `第 ${i + 1} 段（${SEGMENT_LABELS[i]}）格式错误：须小写字母开头+小写字母数字下划线`;
    }
  }
  return null;
}
