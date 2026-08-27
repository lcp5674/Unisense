import { useState } from "react";
import { Button, Tag, Tooltip, message } from "antd";
import { CopyOutlined } from "@ant-design/icons";

// —— 长标识值（指标编码 / 表名 / 节点 ID 等）统一展示组件 ——
// 超长连续编码（如 outp_e2e_fee_day_2026_q3_retail_amount_sum_by_store_region）若整行换行会撑高卡片/表格列且视觉杂乱。
// 统一策略：
//   - 超长 → 「单行中间省略」保留首尾段（首段便于识别前缀模式，尾段保留编码结尾特征）
//   - hover/focus → Tooltip 展示完整值 + 一键复制（完整值同时存于 aria-label，屏幕阅读器友好）
//   - 可点击 → 传入 target/onNavigate 渲染为链接样式直达详情
//   - 视觉变体 → code（等宽浅底）/ tag（边框 Tag）可选
//   - 极端长值（>100）→ Tooltip 内纵向滚动兜底，避免弹层过高遮挡
export const CODE_ELLIPSIS_MAX = 44;
export const CODE_EXTREME_LONG = 100;

export function ellipsizeCode(v: string, max = CODE_ELLIPSIS_MAX): string {
  if (v.length <= max) return v;
  const headLen = Math.ceil(max * 0.6); // 首段占比 60%，便于识别编码前缀模式
  const tailLen = max - headLen - 1; // 尾部剩余（-1 给省略号），保留编码结尾特征
  return `${v.slice(0, headLen)}…${v.slice(-tailLen)}`;
}

export function CodeValue({
  value,
  displayValue,
  target,
  onNavigate,
  code,
  tag,
  maxWidth,
  maxChars = CODE_ELLIPSIS_MAX,
}: {
  value: string;
  /** 展示文本（可选）：剥离前缀后的编码本体，用于窄列下省略更多有效信息；完整值仍存于 aria-label / Tooltip */
  displayValue?: string;
  /** 跳转目标（如 /detail/xxx），与 onNavigate 搭配使用 */
  target?: string | null;
  /** 点击跳转回调；传入后渲染为链接样式（stopPropagation 防卡片冒泡） */
  onNavigate?: (t: string) => void;
  /** code 变体：等宽字体 + 浅底（订阅表格 / 编码列等场景） */
  code?: boolean;
  /** tag 变体：边框 Tag 视觉（详情页依赖指标等场景） */
  tag?: boolean;
  /** 省略后最大宽度（px）；默认 code 变体 420 / 其余 260，表格窄列可传更小值 */
  maxWidth?: number;
  /** 省略阈值（字符数）；默认 44。窄列（如等宽节点列）可传更小值，保证省略文本完整落在容器内不二次截断 */
  maxChars?: number;
}) {
  const shown = displayValue ?? value;
  const isLong = shown.length > maxChars;
  const isExtreme = value.length > CODE_EXTREME_LONG;
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      message.success(`已复制：${value.length > 60 ? `${value.slice(0, 60)}…` : value}`);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用时静默，不阻断交互 */
    }
  };
  const onClick = onNavigate
    ? (e: React.MouseEvent) => {
        e.stopPropagation();
        onNavigate(target as string);
      }
    : undefined;
  const display = isLong ? ellipsizeCode(shown, maxChars) : shown;

  const tooltipTitle = (
    <div className="code-value-tip">
      <div className={isExtreme ? "code-value-tip-text code-value-tip-text-extreme" : "code-value-tip-text"}>
        {value}
      </div>
      <Button size="small" type="link" icon={<CopyOutlined />} onClick={copy}>
        {copied ? "已复制" : "复制"}
      </Button>
    </div>
  );

  // Tag 变体：保留边框视觉（详情页依赖指标等），内部文本中间省略 + CSS 二次兜底
  if (tag) {
    const tagNode = (
      <Tag
        className={`code-value code-value-tag${isLong ? " code-value-long" : ""}`}
        style={{ maxWidth: maxWidth ?? 260 }}
        aria-label={value}
        onClick={onClick}
        title={value}
      >
        {display}
      </Tag>
    );
    if (!isLong) return tagNode;
    return (
      <Tooltip trigger={["hover", "focus"]} placement="topLeft" mouseEnterDelay={0.15} mouseLeaveDelay={0.15} classNames={{ root: "code-value-tip-overlay" }} title={tooltipTitle}>
        {tagNode}
      </Tooltip>
    );
  }

  const cls = [
    "code-value",
    code ? "code-value-code" : "",
    isLong ? "code-value-long" : "",
    onNavigate ? "code-value-link" : "",
  ].filter(Boolean).join(" ");
  const TagEl = code ? "code" : "span";
  const inner = (
    <TagEl
      className={cls}
      aria-label={value}
      onClick={onClick}
      // maxWidth 内联覆盖：表格窄列等场景把长编码约束到指定宽度（覆盖 .code-value-long 默认 420px），
      // 避免撑破列宽；省略文本超宽时由 CSS ellipsis 二次截断兜底
      style={maxWidth !== undefined ? { maxWidth } : undefined}
    >
      {display}
    </TagEl>
  );
  if (!isLong) return inner;
  return (
    <Tooltip
      trigger={["hover", "focus"]}
      placement="topLeft"
      mouseEnterDelay={0.15}
      mouseLeaveDelay={0.15}
      classNames={{ root: isExtreme ? "code-value-tip-overlay code-value-tip-overlay-wide" : "code-value-tip-overlay" }}
      title={tooltipTitle}
    >
      {inner}
    </Tooltip>
  );
}
