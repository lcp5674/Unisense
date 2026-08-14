import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Button, Input, App as AntApp } from "antd";
import { apiLogin, clearToken, fetchCurrentUser, getToken, UnisenseApiError } from "./api";
import type { CurrentUser } from "./types";
import { Layout } from "./components/Layout";
import { MetricCatalog } from "./pages/MetricCatalog";
import { GlobalSearch } from "./pages/GlobalSearch";
import { MetricDetail } from "./pages/MetricDetail";
import { MetricCompare } from "./pages/MetricCompare";
import { MetricCreate } from "./pages/MetricCreate";
import { ReviewWorkbench } from "./pages/ReviewWorkbench";
import { MetricReview } from "./pages/MetricReview";
import { TodoCenter } from "./pages/TodoCenter";
import { LineageView } from "./pages/LineageView";
import { Favorites } from "./pages/Favorites";
import { Dashboard } from "./pages/Dashboard";
import { ConsumptionGuide } from "./pages/ConsumptionGuide";
import { AssetMap } from "./pages/AssetMap";
import { Templates } from "./pages/Templates";
import { QueryWorkspace } from "./pages/QueryWorkspace";
import { ApiClients } from "./pages/ApiClients";
import { Dimensions } from "./pages/Dimensions";
import { Glossary } from "./pages/Glossary";
import { Governance } from "./pages/Governance";
import { QualityCenter } from "./pages/QualityCenter";
import { Notifications } from "./pages/Notifications";
import { Observability } from "./pages/Observability";
import { AiAssistant } from "./pages/AiAssistant";
import { SystemConfig } from "./pages/SystemConfig";
import { UserManagement } from "./pages/UserManagement";
import { AuditLog } from "./pages/AuditLog";
import { DataSources } from "./pages/DataSources";
import { Catalogs } from "./pages/Catalogs";
import { CollectionTasks } from "./pages/CollectionTasks";
import { CollectionHistory } from "./pages/CollectionHistory";
import { SubjectDomain } from "./pages/SubjectDomain";
import { SystemDict } from "./pages/SystemDict";
import { TrackingProvider } from "./components/TrackingProvider";

const { useApp } = AntApp;

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
        <div className="login-brand">
          <div className="brand-mark">U</div>
          <div>
            <div className="brand-name">Unisense</div>
            <div className="brand-sub">Metric Semantics Hub</div>
          </div>
        </div>

        <div className="login-hero">
          <h2>
            一套口径，<br />
            全员 <em>校准</em>。
          </h2>
          <p>
            Unisense 指标语义中台让组织里的每一个指标——
            定义、血缘、治理、消费——都可精确校准、可信追溯。
          </p>
        </div>

        <div className="login-value">
          <div className="val">
            <span className="val-num">100%</span>
            <span className="val-label">口径统一<br />一套定义，全员对齐</span>
          </div>
          <div className="val">
            <span className="val-num">0</span>
            <span className="val-label">歧义灰度<br />血缘与变更影响全程可溯</span>
          </div>
          <div className="val">
            <span className="val-num">24×7</span>
            <span className="val-label">治理守护<br />PII 合规与质量告警实时在线</span>
          </div>
        </div>

        <div className="login-foot">Unisense · Metric Semantics Hub v0.1</div>
      </div>

      <div className="login-panel">
        <form className="login-card" onSubmit={handleSubmit}>
          <div className="login-kicker">Sign in</div>
          <h1>登录工作台</h1>
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
            {loading ? "正在校准…" : "进入工作台"}
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
      .catch(() => clearToken())
      .finally(() => setBooting(false));
  }, []);

  if (booting) {
    return (
      <div className="login-wrap">
        <div className="login-panel">
          <div className="login-card" style={{ textAlign: "center" }}>
            <div className="brand-mark" style={{ margin: "0 auto 16px" }}>U</div>
            <div className="muted">正在校准工作台…</div>
          </div>
        </div>
      </div>
    );
  }
  if (!user) return <LoginPage onLogin={setUser} />;

  return (
    <TrackingProvider user={user}>
      <BrowserRouter>
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
            <Route path="/create" element={<MetricCreate />} />
            <Route path="/metrics/review" element={<MetricReview />} />
            <Route path="/favorites" element={<Favorites />} />
            <Route path="/assetmap" element={<AssetMap />} />
            <Route path="/lineage" element={<LineageView />} />
            <Route path="/review" element={<ReviewWorkbench />} />
            <Route path="/quality" element={<QualityCenter />} />
            <Route path="/dimensions" element={<Dimensions />} />
            <Route path="/glossary" element={<Glossary />} />
            <Route path="/governance" element={<Governance />} />
            <Route path="/audit" element={<AuditLog />} />
            <Route path="/query" element={<QueryWorkspace />} />
            <Route path="/api-clients" element={<ApiClients />} />
            <Route path="/ai" element={<AiAssistant />} />
            <Route path="/system-config" element={<SystemConfig />} />
            <Route path="/users" element={<UserManagement />} />
            <Route path="/observability" element={<Observability />} />
            <Route path="/data-sources" element={<DataSources />} />
            <Route path="/catalogs" element={<Catalogs />} />
            <Route path="/collection-tasks" element={<CollectionTasks />} />
            <Route path="/collection-history" element={<CollectionHistory />} />
            <Route path="/domains" element={<SubjectDomain />} />
            <Route path="/dicts" element={<SystemDict />} />
            <Route path="/guide/:metricCode" element={<ConsumptionGuide />} />
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </TrackingProvider>
  );
}
