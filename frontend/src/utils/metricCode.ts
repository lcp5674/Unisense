/**
 * 指标编码校验（与后端 ConflictPrechecker.validate_code_format 保持一致）：
 * 4 段式「域_业务对象_度量_统计周期」。域段可含下划线（域编码如
 * `online_consultation` 本身带下划线）——字面 split 后 >4 段时，取最后 3 段为
 * 业务对象/度量/周期（须无下划线），前面所有段合并为域段；系统生成侧已把域段
 * 去下划线（字面恰好 4 段），此处宽容手写带下划线域的编码。
 * 留空返回 null（由后端自动生成），显式值非法返回错误信息。
 *
 * 注意：不能依赖整串正则判定 4 段——`[a-z0-9_]*` 会吞段间下划线（如 a_b_c_d_e
 * 5 段也能被整串正则匹配），必须与后端一致用 split("_") 数段再逐段校验。
 */

const CODE_SEGMENT_PATTERN = /^[a-z][a-z0-9_]*$/;
// 后 3 段（业务对象/度量/统计周期）段格式：段内无下划线（4 段式以 _ 分隔）
const TAIL_SEGMENT_PATTERN = /^[a-z][a-z0-9]*$/;

// 供测试/展示参考（非 4 段判定依据，见上方注释）
export const METRIC_CODE_PATTERN =
  /^[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*$/;

const SEGMENT_LABELS = ["域", "业务对象", "度量", "统计周期"];

export function validateMetricCode(code: string | undefined | null): string | null {
  if (!code || !code.trim()) return null; // 留空 → 系统自动生成
  const c = code.trim();
  const parts = c.split("_");
  if (parts.length < 4) {
    return `须符合 4 段格式（域_业务对象_度量_统计周期），当前 ${parts.length} 段`;
  }
  if (parts.length > 4) {
    // 域段可含下划线：最后 3 段须无下划线（业务对象/度量/周期段规范无下划线），
    // 前面所有段合并为域段
    const tail = parts.slice(-3);
    const domainSeg = parts.slice(0, -3).join("_");
    for (let i = 0; i < tail.length; i += 1) {
      if (!TAIL_SEGMENT_PATTERN.test(tail[i])) {
        return `后 3 段（业务对象_度量_统计周期）格式错误：须小写字母开头+小写字母数字（无下划线）`;
      }
    }
    if (!CODE_SEGMENT_PATTERN.test(domainSeg)) {
      return `第 1 段（域）格式错误：须小写字母开头+小写字母数字下划线`;
    }
    return null;
  }
  for (let i = 0; i < parts.length; i += 1) {
    if (!CODE_SEGMENT_PATTERN.test(parts[i])) {
      return `第 ${i + 1} 段（${SEGMENT_LABELS[i]}）格式错误：须小写字母开头+小写字母数字下划线`;
    }
  }
  return null;
}
