// WeSemantic 设计系统 — 「校准仪表」身份
// 六色令牌系统：ink / chassis / paper / surface / line / signal / data
// signal（琥珀）是品牌主色与活体读数，唯一的视觉冒险；其余保持安静克制。
// 详见 docs/DEV_GUIDE.md 与 frontend-design 设计提案。

import type { ThemeConfig } from "antd";

export const TOKENS = {
  /** 深墨蓝：主文字 / 暗色机箱 */
  ink: "#0C1626",
  /** 侧边导航「仪表轨道」底色（比 ink 略亮，保证可读性） */
  chassis: "#101E33",
  /** 页面冷灰底（非奶油色） */
  paper: "#F5F6F8",
  /** 卡片 / 面板 */
  surface: "#FFFFFF",
  /** 发丝分割线 */
  line: "#E3E7EE",
  /** 琥珀色信号灯：品牌主色、激活态、活体读数 */
  signal: "#E8862D",
  /** 青绿色：图表 / 数据可视化 */
  data: "#0E7C86",
  /** 次级文字 */
  muted: "#5B6472",
  /** 面板上的次级边框 */
  lineSoft: "#EEF1F5",
  /** 成功 / 正常读数 */
  ok: "#2E9E5B",
  /** 告警读数 */
  warn: "#C77700",
  /** 危险读数 */
  danger: "#D64545",
  /** 等宽数据字体栈 */
  fontMono:
    '"JetBrains Mono", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
  /** 中文正文栈（不引 webfont，避免内网加载失败） */
  fontBody:
    '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", Roboto, sans-serif',
  /** 品牌展示字体（拉丁） */
  fontDisplay: '"Space Grotesk", "PingFang SC", -apple-system, sans-serif',
} as const;

/** antd v5 ConfigProvider 主题 */
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: TOKENS.signal,
    colorInfo: TOKENS.data,
    colorSuccess: TOKENS.ok,
    colorWarning: TOKENS.warn,
    colorError: TOKENS.danger,
    colorBgLayout: TOKENS.paper,
    colorBgContainer: TOKENS.surface,
    colorText: TOKENS.ink,
    colorTextSecondary: TOKENS.muted,
    colorBorder: TOKENS.line,
    colorBorderSecondary: TOKENS.lineSoft,
    borderRadius: 8,
    borderRadiusLG: 12,
    fontSize: 14,
    fontFamily: TOKENS.fontBody,
    controlHeight: 36,
    boxShadowSecondary: "0 6px 24px rgba(12,22,38,0.08)",
  },
  components: {
    Layout: {
      headerBg: TOKENS.surface,
      siderBg: TOKENS.chassis,
      headerHeight: 56,
      headerPadding: "0 24px",
    },
    Menu: {
      darkItemBg: TOKENS.chassis,
      darkSubMenuItemBg: "rgba(12,22,38,0.35)",
      darkItemSelectedBg: "rgba(232,134,45,0.16)",
      darkItemSelectedColor: "#F2A45C",
      darkItemColor: "rgba(235,240,247,0.72)",
      darkItemHoverColor: "#FFFFFF",
      itemBorderRadius: 8,
      itemMarginInline: 10,
    },
    Table: {
      headerBg: "#F8F9FB",
      headerColor: TOKENS.ink,
      rowHoverBg: "#FBF6EF",
    },
    Card: {
      borderRadiusLG: 12,
    },
    Statistic: {
      contentFontSize: 28,
    },
  },
};
