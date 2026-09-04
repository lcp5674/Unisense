/**
 * 指标编码校验（与后端 ConflictPrechecker.validate_code_format 保持一致）：
 * 通用小写标识符——小写字母开头、仅含小写字母/数字/下划线、不以下划线开头/
 * 结尾、无连续下划线。
 *
 * 2026-09 用户反馈放宽：不再强制「域_业务对象_度量_统计周期」4 段式——4 段式
 * 保留为系统自动生成的命名建议（按域/源表/度量列/周期拼接），手输编码只需
 * 满足通用标识符即可注册。留空返回 null（由后端自动生成），非法返回错误信息。
 */

const CODE_PATTERN = /^[a-z][a-z0-9_]*$/;

// 供测试/展示参考（历史 4 段式形态；现校验已放宽为通用标识符）
export const METRIC_CODE_PATTERN =
  /^[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*$/;

export function validateMetricCode(code: string | undefined | null): string | null {
  if (!code || !code.trim()) return null; // 留空 → 系统自动生成
  const c = code.trim();
  if (!CODE_PATTERN.test(c)) {
    return "编码须以小写字母开头，仅含小写字母、数字和下划线";
  }
  if (c.startsWith("_") || c.endsWith("_") || c.includes("__")) {
    return "编码不能以下划线开头/结尾，且不允许连续下划线";
  }
  return null;
}
