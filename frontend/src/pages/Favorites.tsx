import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Input, List, message, Space, Tag } from "antd";
import { HeartOutlined, DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { addFavorite, listFavorites, removeFavorite, getMetric, UnisenseApiError } from "../api";
import { useTracking } from "../hooks/useTracking";

export function Favorites() {
  const [codes, setCodes] = useState<string[]>([]);
  const [names, setNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [newCode, setNewCode] = useState("");
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      const favs = await listFavorites();
      setCodes(favs);
      const nameMap: Record<string, string> = {};
      await Promise.all(
        favs.map(async (c) => {
          try {
            const m = await getMetric(c);
            nameMap[c] = m.name;
          } catch {
            nameMap[c] = c;
          }
        }),
      );
      setNames(nameMap);
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
        dataSource={codes}
        locale={{ emptyText: "暂无收藏" }}
        renderItem={(c) => (
          <List.Item
            actions={[
              <Button
                type="link"
                key="open"
                icon={<HeartOutlined />}
                onClick={() => navigate(`/detail/${c}`)}
              >
                查看
              </Button>,
              <Button
                type="link"
                key="remove"
                danger
                icon={<DeleteOutlined />}
                onClick={() => handleRemove(c)}
              >
                移除
              </Button>,
            ]}
          >
            <List.Item.Meta
              title={names[c] ?? c}
              description={<Tag>{c}</Tag>}
            />
          </List.Item>
        )}
      />
    </Card>
  );
}
