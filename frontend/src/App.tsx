import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { apiLogin, clearToken, fetchCurrentUser, getToken, UnisenseApiError } from "./api";
import type { CurrentUser } from "./types";
import { Layout } from "./components/Layout";
import { MetricCatalog } from "./pages/MetricCatalog";
import { MetricDetail } from "./pages/MetricDetail";
import { MetricCreate } from "./pages/MetricCreate";
import { ReviewWorkbench } from "./pages/ReviewWorkbench";
import { TodoCenter } from "./pages/TodoCenter";
import { LineageView } from "./pages/LineageView";
import { Favorites } from "./pages/Favorites";
import { Dashboard } from "./pages/Dashboard";
import { ConsumptionGuide } from "./pages/ConsumptionGuide";
import { TrackingProvider } from "./components/TrackingProvider";

function LoginPage({ onLogin }: { onLogin: (u: CurrentUser) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await apiLogin(username, password);
      const me = await fetchCurrentUser();
      onLogin(me);
    } catch (err) {
      setError(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1>Unisense 登录</h1>
        <p className="muted">指标语义中台</p>
        <label>
          用户名
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        </label>
        <label>
          密码
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <div className="error-box">{error}</div>}
        <button type="submit" disabled={loading}>
          {loading ? "登录中…" : "登录"}
        </button>
      </form>
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

  if (booting) return <div className="login-wrap">加载中…</div>;
  if (!user) return <LoginPage onLogin={setUser} />;

  return (
    <TrackingProvider user={user}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout user={user} />}>
            <Route path="/catalog" element={<MetricCatalog />} />
            <Route path="/detail/:code" element={<MetricDetail />} />
            <Route path="/create" element={<MetricCreate />} />
            <Route path="/review" element={<ReviewWorkbench />} />
            <Route path="/todo" element={<TodoCenter />} />
            <Route path="/lineage" element={<LineageView />} />
            <Route path="/favorites" element={<Favorites />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/guide/:metricCode" element={<ConsumptionGuide />} />
            <Route path="/" element={<Navigate to="/catalog" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </TrackingProvider>
  );
}
