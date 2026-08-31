import {
  Component, lazy, Suspense, useEffect, useState, type ComponentType, type ReactNode,
} from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Button, Input, App as AntApp } from "antd";
import {
  apiLogin,
  AUTH_EXPIRED_EVENT,
  clearAuthTokens,
  fetchCurrentUser,
  getToken,
  UnisenseApiError,
} from "./api";
import type { CurrentUser } from "./types";
import { Layout } from "./components/Layout";
import { PermissionProvider, RequirePerm, ROUTE_PERM } from "./hooks/usePermission";

//: 页面级代码分割（P0-4）：33 个页面含 G6/图表静态打进首屏包，改为 React.lazy 按需加载。
//: 页面均为具名导出，用 lazyNamed 包装为 default 供 React.lazy 消费。
const lazyNamed = (loader: () => Promise<Record<string, unknown>>, name: string) =>
  lazy(() => loader().then((m) => ({ default: m[name] as ComponentType })));

const MetricCatalog = lazyNamed(() => import("./pages/MetricCatalog"), "MetricCatalog");
const GlobalSearch = lazyNamed(() => import("./pages/GlobalSearch"), "GlobalSearch");
const MetricDetail = lazyNamed(() => import("./pages/MetricDetail"), "MetricDetail");
const MetricCompare = lazyNamed(() => import("./pages/MetricCompare"), "MetricCompare");
const MetricCreate = lazyNamed(() => import("./pages/MetricCreate"), "MetricCreate");
const MeasureCatalogs = lazyNamed(() => import("./pages/MeasureCatalogs"), "MeasureCatalogs");
const SqlInferEval = lazyNamed(() => import("./pages/SqlInferEval"), "SqlInferEval");
const ApprovalCenter = lazyNamed(() => import("./pages/ApprovalCenter"), "ApprovalCenter");
const TodoCenter = lazyNamed(() => import("./pages/TodoCenter"), "TodoCenter");
const LineageView = lazyNamed(() => import("./pages/LineageView"), "LineageView");
const Favorites = lazyNamed(() => import("./pages/Favorites"), "Favorites");
const Dashboard = lazyNamed(() => import("./pages/Dashboard"), "Dashboard");
const ConsumptionGuide = lazyNamed(() => import("./pages/ConsumptionGuide"), "ConsumptionGuide");
const AssetMap = lazyNamed(() => import("./pages/AssetMap"), "AssetMap");
const Templates = lazyNamed(() => import("./pages/Templates"), "Templates");
const QueryWorkspace = lazyNamed(() => import("./pages/QueryWorkspace"), "QueryWorkspace");
const ApiClients = lazyNamed(() => import("./pages/ApiClients"), "ApiClients");
const Dimensions = lazyNamed(() => import("./pages/Dimensions"), "Dimensions");
const Glossary = lazyNamed(() => import("./pages/Glossary"), "Glossary");
const Governance = lazyNamed(() => import("./pages/Governance"), "Governance");
const QualityCenter = lazyNamed(() => import("./pages/QualityCenter"), "QualityCenter");
const Notifications = lazyNamed(() => import("./pages/Notifications"), "Notifications");
const Observability = lazyNamed(() => import("./pages/Observability"), "Observability");
const FeedbackCenter = lazyNamed(() => import("./pages/FeedbackCenter"), "FeedbackCenter");
const TrackingStats = lazyNamed(() => import("./pages/TrackingStats"), "TrackingStats");
const AiAssistant = lazyNamed(() => import("./pages/AiAssistant"), "AiAssistant");
const SystemConfig = lazyNamed(() => import("./pages/SystemConfig"), "SystemConfig");
const UserManagement = lazyNamed(() => import("./pages/UserManagement"), "UserManagement");
const OrgManagement = lazyNamed(() => import("./pages/OrgManagement"), "OrgManagement");
const AuditLog = lazyNamed(() => import("./pages/AuditLog"), "AuditLog");
const DataSources = lazyNamed(() => import("./pages/DataSources"), "DataSources");
const Catalogs = lazyNamed(() => import("./pages/Catalogs"), "Catalogs");
const CollectionTasks = lazyNamed(() => import("./pages/CollectionTasks"), "CollectionTasks");
const CollectionHistory = lazyNamed(() => import("./pages/CollectionHistory"), "CollectionHistory");
const SubjectDomain = lazyNamed(() => import("./pages/SubjectDomain"), "SubjectDomain");
const SystemDict = lazyNamed(() => import("./pages/SystemDict"), "SystemDict");
const SensitiveRules = lazyNamed(() => import("./pages/SensitiveRules"), "SensitiveRules");
const Account = lazyNamed(() => import("./pages/Account"), "Account");
import { TrackingProvider } from "./components/TrackingProvider";

const { useApp } = AntApp;

//: 全局错误边界（P0-4）：任一页面/组件运行时异常不再整站白屏，降级为可恢复的
//: 错误提示页（含重新加载），并记录 console.error 便于排查。
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }) {
    console.error("WeSemantics UI ErrorBoundary:", error, info.componentStack);
  }

  handleReload = () => {
    this.setState({ error: null });
    window.location.reload();
  };

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            display: "flex", flexDirection: "column", alignItems: "center",
            justifyContent: "center", minHeight: "60vh", gap: 16, padding: 24,
          }}
        >
          <div style={{ fontSize: 18, fontWeight: 600 }}>页面渲染出现异常</div>
          <div className="muted" style={{ maxWidth: 520, textAlign: "center" }}>
            {String(this.state.error.message || this.state.error)}
          </div>
          <Button type="primary" onClick={this.handleReload}>重新加载</Button>
        </div>
      );
    }
    return this.props.children;
  }
}

//: 懒加载页面的加载占位（与登录加载态视觉一致）
function PageLoading() {
  return (
    <div
      style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        minHeight: "60vh", gap: 12, color: "#6b7280", fontSize: 14,
      }}
    >
      <div className="brand-mark" style={{ width: 28, height: 28, fontSize: 14 }}>W</div>
      正在加载模块…
    </div>
  );
}

// 登录页：左 55% 墨蓝机箱 + 发丝计量网格 + 品牌价值点；右 45% 纸上表单
function LoginPage({ onLogin }: { onLogin: (u: CurrentUser) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const { message } = useApp();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) {
      message.warning("请输入用户名和密码");
      return;
    }
    setLoading(true);
    try {
      await apiLogin(username.trim(), password);
      const me = await fetchCurrentUser();
      onLogin(me);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-chassis">
        {/* 血缘流动拓扑背景（纯装饰层，零交互逻辑改动）：数据源→指标→治理→消费 */}
        <svg
          className="login-network"
          viewBox="0 0 800 600"
          preserveAspectRatio="xMidYMid slice"
          aria-hidden="true"
        >
          <g className="ws-net-edges">
            <path
              d="M130 150 C 220 150, 270 190, 350 210"
              className="ws-edge ws-edge-src"
              style={{ animationDelay: "0s", animationDuration: "2.6s" }}
            />
            <path
              d="M180 280 C 250 260, 290 230, 350 210"
              className="ws-edge ws-edge-src"
              style={{ animationDelay: "0.4s", animationDuration: "3.2s" }}
            />
            <path
              d="M180 280 C 260 300, 300 340, 370 360"
              className="ws-edge ws-edge-src"
              style={{ animationDelay: "0.8s", animationDuration: "2.9s" }}
            />
            <path
              d="M140 420 C 230 420, 290 390, 370 360"
              className="ws-edge ws-edge-src"
              style={{ animationDelay: "1.2s", animationDuration: "3.5s" }}
            />
            <path
              d="M350 210 C 440 170, 500 150, 580 150"
              className="ws-edge ws-edge-mid"
              style={{ animationDelay: "0.2s", animationDuration: "3.8s" }}
            />
            <path
              d="M350 210 C 450 240, 500 320, 590 340"
              className="ws-edge ws-edge-mid"
              style={{ animationDelay: "0.6s", animationDuration: "4.2s" }}
            />
            <path
              d="M370 360 C 450 370, 500 350, 590 340"
              className="ws-edge ws-edge-mid"
              style={{ animationDelay: "1s", animationDuration: "3.6s" }}
            />
            <path
              d="M580 150 C 640 180, 670 230, 720 250"
              className="ws-edge ws-edge-dst"
              style={{ animationDelay: "0.5s", animationDuration: "4.5s" }}
            />
            <path
              d="M590 340 C 640 310, 670 270, 720 250"
              className="ws-edge ws-edge-dst"
              style={{ animationDelay: "0.9s", animationDuration: "4s" }}
            />
          </g>
          <g className="ws-net-nodes">
            <circle cx="130" cy="150" r="5" className="ws-node ws-node-src" style={{ animationDelay: "0s" }} />
            <circle cx="180" cy="280" r="5" className="ws-node ws-node-src" style={{ animationDelay: "0.5s" }} />
            <circle cx="140" cy="420" r="5" className="ws-node ws-node-src" style={{ animationDelay: "1s" }} />
            <circle cx="350" cy="210" r="6.5" className="ws-node ws-node-mid" style={{ animationDelay: "0.3s" }} />
            <circle cx="370" cy="360" r="6.5" className="ws-node ws-node-mid" style={{ animationDelay: "0.9s" }} />
            <circle cx="580" cy="150" r="5" className="ws-node ws-node-gov" style={{ animationDelay: "0.6s" }} />
            <circle cx="590" cy="340" r="5" className="ws-node ws-node-gov" style={{ animationDelay: "1.3s" }} />
            <circle cx="720" cy="250" r="6" className="ws-node ws-node-dst" style={{ animationDelay: "0.2s" }} />
            {/* 指标层信号辐射波纹（指标被持续消费/信任传播） */}
            <circle cx="350" cy="210" r="9" className="ws-ripple" style={{ animationDelay: "0.4s" }} />
            <circle cx="370" cy="360" r="9" className="ws-ripple" style={{ animationDelay: "1.4s" }} />
          </g>
        </svg>

        <div className="login-brand">
          <div className="brand-mark">W</div>
          <div>
            <div className="brand-name">WeSemantics</div>
            <div className="brand-sub">Metric Semantics Hub</div>
          </div>
        </div>

        <div className="login-hero">
          <h2>
            一次定义，<br />
            处处 <em>可信</em>。
          </h2>
          <p>
            WeSemantics 以语义为锚，把组织里散落的指标口径编织成一张可信之网——
            定义、血缘、治理、消费，全链路一致、可解释、可追溯。
          </p>
        </div>

        <div className="login-value">
          <div className="val">
            <span className="val-num">语义统一</span>
            <span className="val-label">OneData 逻辑度量<br />一套定义，全员对齐</span>
          </div>
          <div className="val">
            <span className="val-num">血缘可溯</span>
            <span className="val-label">指标 → 落地表全链路<br />变更影响一目了然</span>
          </div>
          <div className="val">
            <span className="val-num">治理可信</span>
            <span className="val-label">PII 合规 · 质量告警<br />分级管控实时在线</span>
          </div>
          <div className="val">
            <span className="val-num">AI 智能</span>
            <span className="val-label">SQL 智能推断 · 口径生成<br />AI 助手全程辅助</span>
          </div>
        </div>

        <div className="login-footbar">
          <span className="footbar-brand">WeSemantics · 指标语义中台</span>
          <span className="footbar-sep" />
          <span className="footbar-trust">
            <span className="chip">全程操作审计</span>
            <i className="dot" />
            <span className="chip">变更灰度发布</span>
            <i className="dot" />
            <span className="chip">软删可恢复</span>
          </span>
        </div>
      </div>

      <div className="login-panel">
        <form className="login-card" onSubmit={handleSubmit}>
          <div className="login-kicker">Welcome</div>
          <h1>欢迎回来</h1>
          <div className="login-subhead">登录以继续您的指标语义治理工作</div>
          <label>
            用户名
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
              placeholder="请输入用户名"
              size="large"
            />
          </label>
          <label>
            密码
            <Input.Password
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              placeholder="请输入密码"
              size="large"
            />
          </label>
          <Button
            type="primary"
            htmlType="submit"
            loading={loading}
            size="large"
            block
            className="login-submit"
          >
            {loading ? "正在登录…" : "进入语义中台"}
          </Button>
          <div className="login-hint">
            本地默认账号 <span className="mono">admin</span> /{" "}
            <span className="mono">changeme123</span>
            <br />
            生产环境请使用管理员分配的凭据
          </div>
        </form>
      </div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [booting, setBooting] = useState(true);

  useEffect(() => {
    if (!getToken()) {
      setBooting(false);
      return;
    }
    fetchCurrentUser()
      .then((me) => setUser(me))
      .catch(() => clearAuthTokens())
      .finally(() => setBooting(false));
  }, []);

  // S-4（第八轮）：会话中途失效全局回登录页——api.ts 在 401 刷新失败清 token 后派发
  // AUTH_EXPIRED_EVENT，此处监听把 user 置 null（App 守卫 `!user → 登录页` 收敛），
  // 用户不再滞留当前页反复报错。
  useEffect(() => {
    const onAuthExpired = () => setUser(null);
    window.addEventListener(AUTH_EXPIRED_EVENT, onAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onAuthExpired);
  }, []);

  if (booting) {
    return (
      <div className="login-wrap">
        <div className="login-panel">
          <div className="login-card" style={{ textAlign: "center" }}>
            <div className="brand-mark" style={{ margin: "0 auto 16px" }}>W</div>
            <div className="muted">正在准备语义中台…</div>
          </div>
        </div>
      </div>
    );
  }
  if (!user) return <LoginPage onLogin={setUser} />;

  return (
    <TrackingProvider user={user}>
      <PermissionProvider user={user}>
        <BrowserRouter>
          <Suspense fallback={<PageLoading />}>
            <ErrorBoundary>
              <Routes>
                <Route element={<Layout user={user} />}>
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/todo" element={<TodoCenter />} />
              <Route path="/notifications" element={<Notifications />} />
              <Route path="/catalog" element={<MetricCatalog />} />
              <Route path="/search" element={<GlobalSearch />} />
              <Route path="/templates" element={<Templates />} />
              <Route path="/detail/:code" element={<MetricDetail />} />
              <Route path="/compare" element={<MetricCompare />} />
              <Route path="/create" element={<RequirePerm perm={ROUTE_PERM["/create"]}><MetricCreate /></RequirePerm>} />
              <Route path="/approval" element={<ApprovalCenter />} />
              {/* 深链兼容：原三个审批/仲裁入口重定向到统一审批中心对应 Tab（保留书签/通知/旧链接） */}
              <Route path="/metrics/review" element={<Navigate to="/approval?tab=metrics" replace />} />
              <Route path="/master-data/review" element={<Navigate to="/approval?tab=master-data" replace />} />
              <Route path="/favorites" element={<Favorites />} />
              <Route path="/assetmap" element={<AssetMap />} />
              <Route path="/lineage" element={<LineageView />} />
              <Route path="/review" element={<Navigate to="/approval?tab=conflict" replace />} />
              <Route path="/quality" element={<QualityCenter />} />
              <Route path="/dimensions" element={<Dimensions />} />
              <Route path="/measure-catalogs" element={<RequirePerm perm={ROUTE_PERM["/measure-catalogs"]}><MeasureCatalogs /></RequirePerm>} />
              <Route path="/sql-infer-eval" element={<RequirePerm perm={ROUTE_PERM["/sql-infer-eval"]}><SqlInferEval /></RequirePerm>} />
              <Route path="/glossary" element={<Glossary />} />
              <Route path="/governance" element={<RequirePerm perm={ROUTE_PERM["/governance"]}><Governance /></RequirePerm>} />
              <Route path="/audit" element={<RequirePerm perm={ROUTE_PERM["/audit"]}><AuditLog /></RequirePerm>} />
              <Route path="/query" element={<QueryWorkspace />} />
              <Route path="/api-clients" element={<RequirePerm perm={ROUTE_PERM["/api-clients"]}><ApiClients /></RequirePerm>} />
              <Route path="/ai" element={<AiAssistant />} />
              <Route path="/system-config" element={<RequirePerm perm={ROUTE_PERM["/system-config"]}><SystemConfig /></RequirePerm>} />
              <Route path="/users" element={<RequirePerm perm={ROUTE_PERM["/users"]}><UserManagement /></RequirePerm>} />
              <Route path="/organizations" element={<RequirePerm perm={ROUTE_PERM["/organizations"]}><OrgManagement /></RequirePerm>} />
              <Route path="/observability" element={<RequirePerm perm={ROUTE_PERM["/observability"]}><Observability /></RequirePerm>} />
              <Route path="/feedback" element={<FeedbackCenter />} />
              <Route path="/tracking-stats" element={<RequirePerm perm={ROUTE_PERM["/tracking-stats"]}><TrackingStats /></RequirePerm>} />
              <Route path="/data-sources" element={<RequirePerm perm={ROUTE_PERM["/data-sources"]}><DataSources /></RequirePerm>} />
              <Route path="/catalogs" element={<RequirePerm perm={ROUTE_PERM["/catalogs"]}><Catalogs /></RequirePerm>} />
              <Route path="/collection-tasks" element={<RequirePerm perm={ROUTE_PERM["/collection-tasks"]}><CollectionTasks /></RequirePerm>} />
              <Route path="/collection-history" element={<RequirePerm perm={ROUTE_PERM["/collection-history"]}><CollectionHistory /></RequirePerm>} />
              <Route path="/domains" element={<RequirePerm perm={ROUTE_PERM["/domains"]}><SubjectDomain /></RequirePerm>} />
              <Route path="/dicts" element={<RequirePerm perm={ROUTE_PERM["/dicts"]}><SystemDict /></RequirePerm>} />
              <Route path="/sensitive-rules" element={<RequirePerm perm={ROUTE_PERM["/sensitive-rules"]}><SensitiveRules /></RequirePerm>} />
              <Route path="/account" element={<Account />} />
              <Route path="/guide/:metricCode" element={<ConsumptionGuide />} />
              <Route path="/" element={<Navigate to="/dashboard" replace />} />
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
              </Route>
              </Routes>
            </ErrorBoundary>
          </Suspense>
        </BrowserRouter>
      </PermissionProvider>
    </TrackingProvider>
  );
}
