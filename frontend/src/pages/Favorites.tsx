import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Empty, Input, message, Segmented, Tag } from "antd";
import {
  ApartmentOutlined,
  BookOutlined,
  DeleteOutlined,
  FundOutlined,
  HeartFilled,
  PlusOutlined,
  ProfileOutlined,
  TableOutlined,
} from "@ant-design/icons";
import {
  addFavorite,
  listFavoriteDetails,
  removeFavorite,
  UnisenseApiError,
} from "../api";
import type { FavoriteAssetType, FavoriteDetail } from "../api";
import { useTracking } from "../hooks/useTracking";

const ASSET_TYPE_LABEL: Record<FavoriteAssetType, string> = {
  METRIC: "指标",
  TABLE: "数据表",
  TERM: "术语",
  DIMENSION: "维度",
  TEMPLATE: "指标模板",
};
const ASSET_TYPE_COLOR: Record<FavoriteAssetType, string> = {
  METRIC: "blue",
  TABLE: "geekblue",
  TERM: "purple",
  DIMENSION: "cyan",
  TEMPLATE: "green",
};
// 各资产类型图标（fav-type-icon 的 .t-{type} 色块内展示）
const TYPE_ICON: Record<FavoriteAssetType, ReactNode> = {
  METRIC: <FundOutlined />,
  TABLE: <TableOutlined />,
  TERM: <BookOutlined />,
  DIMENSION: <ApartmentOutlined />,
  TEMPLATE: <ProfileOutlined />,
};

// 各资产状态/敏感级中文标签（数据表展示敏感级，模板展示启用状态）
const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  UNKNOWN: "已失效",
  ACTIVE: "启用",
  INACTIVE: "停用",
  PUBLIC: "公开",
  INTERNAL: "内部",
  CONFIDENTIAL: "机密",
  RESTRICTED: "受限",
  NEEDS_REVIEW: "待复核",
};
const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  PUBLISHED: "success",
  DEPRECATED: "error",
  UNKNOWN: "warning",
  ACTIVE: "success",
  INACTIVE: "default",
  PUBLIC: "default",
  INTERNAL: "blue",
  CONFIDENTIAL: "orange",
  RESTRICTED: "red",
  NEEDS_REVIEW: "gold",
};

function isDead(f: FavoriteDetail): boolean {
  return f.dead || f.status === "UNKNOWN";
}
function statusLabel(f: FavoriteDetail): string {
  return isDead(f) ? "已失效" : (STATUS_LABEL[f.status] ?? f.status);
}
function statusColor(f: FavoriteDetail): string {
  return isDead(f) ? "warning" : (STATUS_COLOR[f.status] ?? "default");
}

// 各资产类型的详情跳转路由（复用各页 URL 直达能力）
function assetTarget(f: FavoriteDetail): string {
  const id = encodeURIComponent(f.asset_id);
  switch (f.asset_type) {
    case "METRIC":
      return `/detail/${id}`;
    case "TABLE":
      return `/catalogs?kw=${id}`;
    case "TERM":
      return `/glossary?kw=${id}&focus=${id}`;
    case "DIMENSION":
      return `/dimensions?kw=${id}`;
    case "TEMPLATE":
      return `/templates?kw=${id}`;
  }
}

function timeAgo(iso: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const days = Math.floor((Date.now() - t) / 86_400_000);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  if (days < 30) return `${days} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

const TABS: { key: FavoriteAssetType | "ALL"; label: string }[] = [
  { key: "ALL", label: "全部" },
  { key: "METRIC", label: "指标" },
  { key: "TABLE", label: "数据表" },
  { key: "TERM", label: "术语" },
  { key: "DIMENSION", label: "维度" },
  { key: "TEMPLATE", label: "模板" },
];

/** 概览统计卡行：每类资产一张卡，一眼看到收藏结构与健康度；点击卡片即切换筛选（Tab）。 */
function FavStatsBar({
  total,
  byType,
  dead,
  active,
  showDead,
  onSelect,
}: {
  total: number;
  byType: Record<string, number>;
  dead: number;
  active: FavoriteAssetType | "ALL";
  showDead: boolean;
  onSelect: (key: FavoriteAssetType | "ALL" | "DEAD") => void;
}) {
  return (
    <div className="fav-stats">
      <div
        className={`fav-stat-card fav-stat-total${active === "ALL" && !showDead ? " active" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => onSelect("ALL")}
        onKeyDown={(e) => e.key === "Enter" && onSelect("ALL")}
      >
        <span className="fav-stat-num">{total}</span>
        <span className="fav-stat-label">收藏总数</span>
      </div>
      {TABS.filter((t) => t.key !== "ALL").map((t) => (
        <div
          key={t.key}
          className={`fav-stat-card${active === t.key ? " active" : ""}`}
          role="button"
          tabIndex={0}
          onClick={() => onSelect(t.key)}
          onKeyDown={(e) => e.key === "Enter" && onSelect(t.key)}
        >
          <span className="fav-stat-num">{byType[t.key] ?? 0}</span>
          <span className="fav-stat-label">{t.label}</span>
        </div>
      ))}
      <div
        className={`fav-stat-card fav-stat-dead${dead ? " has-dead" : ""}${showDead ? " active" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => onSelect("DEAD")}
        onKeyDown={(e) => e.key === "Enter" && onSelect("DEAD")}
      >
        <span className="fav-stat-num">{dead}</span>
        <span className="fav-stat-label">已失效</span>
      </div>
    </div>
  );
}

/** 工具条：资产类型 Tab + 关键词搜索 + 只看失效开关。 */
function FavToolbar({
  tab,
  onTab,
  search,
  onSearch,
  showDead,
  onToggleDead,
}: {
  tab: FavoriteAssetType | "ALL";
  onTab: (v: FavoriteAssetType | "ALL") => void;
  search: string;
  onSearch: (v: string) => void;
  showDead: boolean;
  onToggleDead: () => void;
}) {
  return (
    <div className="fav-toolbar">
      <Segmented
        value={tab}
        onChange={(v) => onTab(v as FavoriteAssetType | "ALL")}
        options={TABS.map((t) => ({ label: t.label, value: t.key }))}
      />
      <Input
        allowClear
        placeholder="搜索名称/编码"
        value={search}
        onChange={(e) => onSearch(e.target.value)}
        style={{ width: 220 }}
      />
      <Button
        size="small"
        type={showDead ? "primary" : "default"}
        onClick={onToggleDead}
      >
        {showDead ? "取消只看失效" : "只看失效"}
      </Button>
    </div>
  );
}

/** 单条收藏卡片：类型图标 + 名称/标签 + 编码/域/时间 + 定义摘要 + 操作。点击卡片任意区域跳转资产详情。 */
function FavCard({
  f,
  onOpen,
  onRemove,
}: {
  f: FavoriteDetail;
  onOpen: () => void;
  onRemove: () => void;
}) {
  const dead = isDead(f);
  return (
    <div
      className={`fav-card${dead ? " fav-card-dead" : ""}`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === "Enter" && onOpen()}
    >
      <div className={`fav-type-icon t-${f.asset_type.toLowerCase()}`}>
        {TYPE_ICON[f.asset_type]}
      </div>
      <div className="fav-main">
        <div className="fav-title">
          <span className="fav-name">{f.name}</span>
          <Tag color={ASSET_TYPE_COLOR[f.asset_type]}>
            {ASSET_TYPE_LABEL[f.asset_type]}
          </Tag>
          <Tag color={statusColor(f)}>{statusLabel(f)}</Tag>
          {f.tier && <Tag>分级 {f.tier}</Tag>}
          {f.is_pii && <Tag color="red">含 PII</Tag>}
        </div>
        <div className="fav-sub">
          <span className="mono">{f.asset_id}</span>
          {f.domain && <span className="fav-domain">{f.domain}</span>}
          {f.created_at && (
            <span className="fav-time">收藏于 {timeAgo(f.created_at)}</span>
          )}
        </div>
        {f.description && <div className="fav-desc">{f.description}</div>}
      </div>
      <div className="fav-actions">
        <Button
          type="link"
          icon={<HeartFilled />}
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
        >
          查看
        </Button>
        <Button
          type="link"
          danger
          icon={<DeleteOutlined />}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
        >
          移除
        </Button>
      </div>
    </div>
  );
}

export function Favorites() {
  const [items, setItems] = useState<FavoriteDetail[]>([]);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<FavoriteAssetType | "ALL">("ALL");
  const [search, setSearch] = useState("");
  const [newCode, setNewCode] = useState("");
  const [showDead, setShowDead] = useState(false);
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      // 一次聚合拉取全部资产详情（后端已消除逐条取名的 N+1，含收藏时间与失效标记）
      const favs = await listFavoriteDetails();
      setItems(favs);
      track("favorites_view", undefined, "favorite");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 弱化的手输框：默认按指标类型添加（用户通常不记得编码，主路径是去目录收藏）
  async function handleAdd() {
    if (!newCode.trim()) return;
    try {
      await addFavorite("METRIC", newCode.trim());
      setNewCode("");
      load();
      message.success("已添加收藏");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "添加失败",
      );
    }
  }

  async function handleRemove(f: FavoriteDetail) {
    try {
      await removeFavorite(f.asset_type, f.asset_id);
      load();
      message.success("已移除收藏");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "移除失败",
      );
    }
  }

  // 概览统计：总数 / 各类型数 / 失效数
  const stats = useMemo(() => {
    const byType: Record<string, number> = {};
    let dead = 0;
    for (const f of items) {
      byType[f.asset_type] = (byType[f.asset_type] ?? 0) + 1;
      if (isDead(f)) dead += 1;
    }
    return { total: items.length, byType, dead };
  }, [items]);

  // 过滤：Tab + 只看失效 + 关键词（名称/编码）
  const filtered = useMemo(() => {
    const kw = search.trim().toLowerCase();
    return items.filter((f) => {
      if (tab !== "ALL" && f.asset_type !== tab) return false;
      if (showDead && !isDead(f)) return false;
      if (
        kw &&
        !(
          f.name.toLowerCase().includes(kw) ||
          f.asset_id.toLowerCase().includes(kw)
        )
      )
        return false;
      return true;
    });
  }, [items, tab, search, showDead]);

  // 统计卡点击：切换到对应类型 Tab；「已失效」只看失效；「收藏总数」回到全部
  function handleStatSelect(key: FavoriteAssetType | "ALL" | "DEAD") {
    if (key === "DEAD") {
      setShowDead(true);
      setTab("ALL");
    } else {
      setTab(key);
      setShowDead(false);
    }
  }

  return (
    <div>
      {/* 页头：与「校准仪表」设计系统统一（kicker + 标题 + 副标题），右侧弱化手输框 */}
      <div className="page-head">
        <div>
          <div className="page-kicker">FAVORITES · 收藏</div>
          <h2>我的收藏</h2>
          <p>集中收藏指标、数据表、术语、维度与指标模板，点击卡片即可直达资产详情。</p>
        </div>
        <div className="fav-add-quick">
          <Input
            placeholder="知道编码？直接输入添加（默认按指标）"
            value={newCode}
            onChange={(e) => setNewCode(e.target.value)}
            onPressEnter={handleAdd}
            style={{ width: 280 }}
            size="small"
          />
          <Button size="small" icon={<PlusOutlined />} onClick={handleAdd}>
            添加
          </Button>
          <span className="muted" style={{ fontSize: 12 }}>
            或到「指标目录」点心形、详情页点「收藏」
          </span>
        </div>
      </div>

      <FavStatsBar
        total={stats.total}
        byType={stats.byType}
        dead={stats.dead}
        active={tab}
        showDead={showDead}
        onSelect={handleStatSelect}
      />

      <FavToolbar
        tab={tab}
        onTab={setTab}
        search={search}
        onSearch={setSearch}
        showDead={showDead}
        onToggleDead={() => setShowDead(!showDead)}
      />

      {filtered.length === 0 ? (
        loading ? (
          <Card loading />
        ) : (
          <Card className="fav-empty">
            <Empty description="暂无收藏">
              <Button type="primary" onClick={() => navigate("/catalog")}>
                去指标目录挑选收藏
              </Button>
            </Empty>
          </Card>
        )
      ) : (
        <div className="fav-list">
          {filtered.map((f) => (
            <FavCard
              key={`${f.asset_type}:${f.asset_id}`}
              f={f}
              onOpen={() => navigate(assetTarget(f))}
              onRemove={() => handleRemove(f)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
