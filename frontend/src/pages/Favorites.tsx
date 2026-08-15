import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Input, List, message, Space, Tag } from "antd";
import { HeartOutlined, DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { addFavorite, removeFavorite, listFavoriteDetails, UnisenseApiError } from "../api";
import type { FavoriteDetail } from "../api";
import { useTracking } from "../hooks/useTracking";

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  PUBLISHED: "success",
  DEPRECATED: "error",
  UNKNOWN: "warning",
};
const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  UNKNOWN: "已失效",
};

export function Favorites() {
  const [items, setItems] = useState<FavoriteDetail[]>([]);
  const [loading, setLoading] = useState(false);
  const [newCode, setNewCode] = useState("");
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      // 一次聚合拉取名称/域/状态（后端已消除逐条 getMetric 的 N+1）
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

  async function handleAdd() {
    if (!newCode.trim()) return;
    try {
      await addFavorite(newCode.trim());
      setNewCode("");
      load();
      message.success("已添加收藏");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "添加失败",
      );
    }
  }

  async function handleRemove(code: string) {
    try {
      await removeFavorite(code);
      load();
      message.success("已移除收藏");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "移除失败",
      );
    }
  }

  return (
    <Card title="我的收藏">
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="指标编码"
          value={newCode}
          onChange={(e) => setNewCode(e.target.value)}
          onPressEnter={handleAdd}
          style={{ width: 200 }}
        />
        <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>
          添加收藏
        </Button>
      </Space>

      <List
        loading={loading}
        dataSource={items}
        locale={{ emptyText: "暂无收藏" }}
        renderItem={(f) => (
          <List.Item
            actions={[
              <Button
                type="link"
                key="open"
                icon={<HeartOutlined />}
                onClick={() => navigate(`/detail/${f.metric_code}`)}
              >
                查看
              </Button>,
              <Button
                type="link"
                key="remove"
                danger
                icon={<DeleteOutlined />}
                onClick={() => handleRemove(f.metric_code)}
              >
                移除
              </Button>,
            ]}
          >
            <List.Item.Meta
              title={
                <span
                  className={f.status === "UNKNOWN" ? "fav-invalid" : "fav-name"}
                  onClick={() => navigate(`/detail/${f.metric_code}`)}
                >
                  {f.name}
                </span>
              }
              description={
                <Space size={8} wrap>
                  <Tag>{f.metric_code}</Tag>
                  {f.domain && <span className="muted">{f.domain}</span>}
                  <Tag color={STATUS_COLOR[f.status]}>{STATUS_LABEL[f.status] ?? f.status}</Tag>
                </Space>
              }
            />
          </List.Item>
        )}
      />
    </Card>
  );
}
