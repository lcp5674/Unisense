import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, Cascader, message, Space, Descriptions } from "antd";
import { PlusOutlined, ArrowLeftOutlined, HeartOutlined, ReadOutlined } from "@ant-design/icons";
import {
  listTemplates,
  createMetric,
  instantiateTemplate,
  listFavorites,
  addFavorite,
  removeFavorite,
  listUsers,
  updateTemplateOwner,
  listDomainTree,
  listDictItems,
  getDomainDefaults,
  UnisenseApiError,
} from "../api";
import type { MetricCreateRequest, MetricTemplate, MetricType, UserBrief, SubjectDomainTreeNode } from "../types";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { enumLabel, METRIC_TYPE_LABEL, GRANULARITY_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, METRIC_TIER_LABEL } from "../utils/enums";

// 域树 → Cascader 选项（对齐注册指标页：树形选择，避免手输域编码）
function treeToCascaderOptions(nodes: SubjectDomainTreeNode[]): any[] {
  return nodes.map((n) => ({
    value: n.code,
    label: `${n.name} (${n.code})`,
    children: n.children.length > 0 ? treeToCascaderOptions(n.children) : undefined,
  }));
}

// 字典项 → Select 选项（对齐注册指标页：粒度/单位等从字典下拉选择，避免手输漂移）
function dictToOptions(items: Array<{ code: string; label: string; status: string }>) {
  return items
    .filter((it) => it.status === "active")
    .map((it) => ({ value: it.code, label: `${it.label} (${it.code})` }));
}

export function Templates() {
  const [searchParams] = useSearchParams();
  const { can } = usePermission();
  // URL 直达参数（?kw=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  // 启用状态下钻（?is_active=，总览仪表「指标模板」资产卡片）作为初始筛选；
  // 默认仅展示启用模板（与原有行为一致），inactive 下钻展示停用模板
  const urlIsActive = searchParams.get("is_active") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [items, setItems] = useState<MetricTemplate[]>([]);
  const [keyword, setKeyword] = useState(urlKw);
  // 搜索输入框即时显示值：与过滤值 keyword 分离——输入不打断浏览/不发请求，回车确认才过滤
  const [inputValue, setInputValue] = useState(urlKw);
  const [isActive, setIsActive] = useState<string>(urlIsActive === "inactive" ? "inactive" : "active");
  const [ownerId, setOwnerId] = useState<number | undefined>(
    urlOwnerId && /^\d+$/.test(urlOwnerId) ? Number(urlOwnerId) : undefined,
  );
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [modalOpen, setModalOpen] = useState(false);
  const [instantiateTarget, setInstantiateTarget] = useState<MetricTemplate | null>(null);
  // 模板收藏（C 层多资产收藏：TEMPLATE）
  const [favCodes, setFavCodes] = useState<Set<string>>(new Set());
  // 责任人人选（模板「负责人」指派下拉）
  const [users, setUsers] = useState<UserBrief[]>([]);
  const [form] = Form.useForm();
  const navigate = useNavigate();
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  const { track } = useTracking();
  // 实例化弹窗选项：域树 + 粒度/单位字典（对齐注册指标页惰性选择，避免手输漂移）
  const [domainOptions, setDomainOptions] = useState<any[]>([]);
  const [granularityOptions, setGranularityOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [unitOptions, setUnitOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 模板详情弹窗（默认口径 / 必填字段 / 描述）
  const [detailTpl, setDetailTpl] = useState<MetricTemplate | null>(null);
  // 域 code → 中文名映射（列表「域」列显示中文名，与指标目录一致）
  const [domainMap, setDomainMap] = useState<Record<string, string>>({});

  // 加载域树与字典项，供实例化弹窗选项（惰性选择原则）
  useEffect(() => {
    listDomainTree()
      .then((tree) => {
        setDomainOptions(treeToCascaderOptions(tree));
        const m: Record<string, string> = {};
        const walk = (nodes: SubjectDomainTreeNode[]) => {
          for (const n of nodes) {
            m[n.code] = n.name;
            if (n.children?.length) walk(n.children);
          }
        };
        walk(tree);
        setDomainMap(m);
      })
      .catch(() => {});
    listDictItems("granularity")
      .then((items) => setGranularityOptions(dictToOptions(items)))
      .catch(() => {});
    listDictItems("unit")
      .then((items) => setUnitOptions(dictToOptions(items)))
      .catch(() => {});
  }, []);

  // 支持从全局搜索栏经 ?kw= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) {
      setKeyword(urlKw);
      setInputValue(urlKw);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 启用状态参数变化（总览仪表「指标模板」资产卡片二次下钻）
  useEffect(() => {
    const next = urlIsActive === "inactive" ? "inactive" : "active";
    if (next !== isActive) setIsActive(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlIsActive]);

  // 响应 URL 责任人参数变化（Owner 责任分布二次下钻）；ownerId 在 load 依赖中自动重查
  useEffect(() => {
    if (urlOwnerId && /^\d+$/.test(urlOwnerId) && Number(urlOwnerId) !== ownerId) {
      setOwnerId(Number(urlOwnerId));
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOwnerId]);

  async function load(overrideKeyword?: string) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      // 默认仅展示启用模板；inactive 时展示停用模板（总览仪表下钻）
      const res = await listTemplates({
        is_active: isActive !== "inactive",
        keyword: (overrideKeyword ?? keyword) || undefined,
        owner_id: ownerId,
        page,
        page_size: pageSize,
      });
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载模板失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片/全局搜索等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  // 当前用户模板收藏（TEMPLATE）供行内收藏按钮判断
  useEffect(() => {
    listFavorites()
      .then((favs) =>
        setFavCodes(
          new Set(favs.filter((f) => f.asset_type === "TEMPLATE").map((f) => f.asset_id)),
        ),
      )
      .catch(() => {});
  }, []);

  // 模板收藏切换（行内心形）
  // 责任人人选：模板「负责人」指派下拉数据源
  useEffect(() => {
    listUsers()
      .then((u) => setUsers(u))
      .catch(() => {});
  }, []);

  // 指派/解除模板责任人（总览仪表 Owner 责任分布跨资产统计的数据来源）
  async function assignOwner(t: MetricTemplate, ownerId: number | null) {
    try {
      const updated = await updateTemplateOwner(t.id, ownerId);
      setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it)));
      message.success(ownerId ? "已指派责任人" : "已解除责任人");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "指派失败",
      );
    }
  }

  async function toggleFavorite(t: MetricTemplate) {
    const fav = favCodes.has(t.code);
    try {
      if (fav) {
        await removeFavorite("TEMPLATE", t.code);
        setFavCodes((prev) => {
          const next = new Set(prev);
          next.delete(t.code);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("TEMPLATE", t.code);
        setFavCodes((prev) => new Set(prev).add(t.code));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败",
      );
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword, isActive, ownerId, page, pageSize]);

  // 实例化选域后预填域默认值（对齐注册指标页 R8）：模板默认口径优先，仅补空字段；
  // 域默认值是可选项，用户可随时覆盖（惰性设计）
  async function handleInstantiateDomainChange(value: (string | number)[]) {
    const code = value?.length ? String(value[value.length - 1]) : "";
    if (!code) return;
    try {
      const defaults = await getDomainDefaults(code);
      if (!defaults || typeof defaults !== "object") return;
      const prefill: Record<string, string> = {};
      for (const [k, v] of Object.entries(defaults)) {
        if (typeof v !== "string" || !v) continue;
        // 仅补当前为空/未填的字典字段（模板默认值优先，不覆盖）
        const current = form.getFieldValue(k);
        if (current === undefined || current === null || current === "") prefill[k] = v;
      }
      if (Object.keys(prefill).length) form.setFieldsValue(prefill);
    } catch {
      /* 域默认值加载失败不影响实例化（模板默认兜底） */
    }
  }

  async function handleCreate(values: Record<string, unknown>) {
    setLoading(true);
    try {
      // 组装指标基础信息（模板实例化时后端会把模板默认口径与用户覆盖合并）
      const payload: MetricCreateRequest = {
        metric_code: values.metric_code ? String(values.metric_code) : undefined,
        name: String(values.name),
        domain: String(values.domain),
        type: (String(values.type) as MetricType) ?? "atomic",
        granularity: String(values.granularity || "day"),
        unit: String(values.unit || ""),
        aggregation: (String(values.aggregation) as MetricCreateRequest["aggregation"]) ?? "SUM",
        time_semantics: (String(values.time_semantics) as MetricCreateRequest["time_semantics"]) ?? "PERIOD",
        freshness: (String(values.freshness) as MetricCreateRequest["freshness"]) ?? "T1",
        dw_layer: (String(values.dw_layer) as MetricCreateRequest["dw_layer"]) ?? "DWS",
        // 模板默认口径优先保留（defaults_json.definition_json）；缺省时补空对象满足后端必填
        definition_json:
          (instantiateTarget?.defaults_json?.definition_json as Record<string, unknown>) ?? {},
      };
      // 从模板实例化：调用专用接口（后端合并模板默认字段）；无模板上下文时退回普通创建指标
      const created = instantiateTarget
        ? await instantiateTemplate(instantiateTarget.id, payload)
        : await createMetric(payload);
      message.success(instantiateTarget ? `已从模板实例化：${created.metric_code}` : `已创建指标：${created.metric_code}`);
      track("template_instantiate", created.metric_code, "template");
      setModalOpen(false);
      navigate(`/detail/${created.metric_code}`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "实例化失败");
    } finally {
      setLoading(false);
    }
  }

  function openInstantiate(tpl: MetricTemplate) {
    setInstantiateTarget(tpl);
    form.resetFields();
    form.setFieldsValue({
      metric_code: tpl.code,
      name: tpl.name,
      domain: tpl.domain,
      type: tpl.type ?? "atomic",
      granularity: tpl.granularity ?? "day",
      unit: tpl.unit ?? "",
      aggregation: tpl.aggregation ?? "SUM",
      time_semantics: tpl.time_semantics ?? "PERIOD",
      freshness: tpl.freshness ?? "T1",
      dw_layer: tpl.dw_layer ?? "DWS",
    });
    setModalOpen(true);
  }

  const columns = [
    { title: "模板编码", dataIndex: "code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
    {
      title: "状态",
      dataIndex: "is_active",
      key: "is_active",
      width: 90,
      render: (v: boolean) => (v ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    { title: "域", dataIndex: "domain", key: "domain", width: 140, render: (v: string) => domainMap[v] ?? v },
    {
      title: "负责人",
      dataIndex: "owner_id",
      key: "owner_id",
      width: 150,
      render: (_: number | null, t: MetricTemplate) => (
        <Select
          size="small"
          style={{ width: 132 }}
          placeholder="未指派"
          value={t.owner_id ?? undefined}
          allowClear
          disabled={!can("template:assign-owner")}
          options={users
            .filter((u) => u.status !== "DISABLED")
            .map((u) => ({ value: u.id, label: u.display_name }))}
          onChange={(next?: number) => assignOwner(t, next ?? null)}
        />
      ),
    },
    { title: "类型", dataIndex: "type", key: "type", width: 100, render: (v: string) => enumLabel(METRIC_TYPE_LABEL, v) },
    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 100, render: (v: string) => enumLabel(GRANULARITY_LABEL, v) },
    { title: "聚合", dataIndex: "aggregation", key: "aggregation", width: 120, render: (v: string) => enumLabel(AGGREGATION_LABEL, v) },
    { title: "时间语义", dataIndex: "time_semantics", key: "time_semantics", width: 110, render: (v: string) => enumLabel(TIME_SEMANTICS_LABEL, v) },
    { title: "新鲜度", dataIndex: "freshness", key: "freshness", width: 90, render: (v: string) => enumLabel(FRESHNESS_LABEL, v) },
    { title: "数仓层", dataIndex: "dw_layer", key: "dw_layer", width: 90, render: (v: string) => enumLabel(DW_LAYER_LABEL, v) },
    { title: "分级", dataIndex: "metric_tier", key: "metric_tier", width: 90, render: (v: string) => <Tag>{enumLabel(METRIC_TIER_LABEL, v)}</Tag> },
    { title: "必填字段", dataIndex: "required_fields", key: "required_fields", render: (v: string[] | null) => (v?.length ? v.join("、") : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "actions",
      width: 140,
      render: (_: unknown, t: MetricTemplate) => (
        <Space size={4} wrap>
          <Button
            type="link"
            icon={<HeartOutlined style={{ color: favCodes.has(t.code) ? "#eb2f96" : undefined }} />}
            onClick={() => toggleFavorite(t)}
          >
            {favCodes.has(t.code) ? "已收藏" : "收藏"}
          </Button>
          <Button type="link" icon={<ReadOutlined />} onClick={() => setDetailTpl(t)}>详情</Button>
          {can("template:instantiate") && (
            <Button type="link" onClick={() => openInstantiate(t)}>实例化指标</Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">指标资产 / 指标模板</div>
          <h2>指标模板</h2>
          <p>标准化的指标创建模板——一键实例化，默认口径自动合并。</p>
        </div>
        <Button icon={<PlusOutlined />} onClick={() => load()} loading={loading}>刷新</Button>
      </div>

      <Card>
        <div style={{ display: "flex", gap: 12, marginBottom: 12 }}>
          <Input.Search
            placeholder="搜索模板编码 / 名称 / 描述"
            allowClear
            style={{ width: 280 }}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onSearch={() => {
              setKeyword(inputValue);
              setPage(1);
            }}
            onClear={() => {
              setInputValue("");
              setKeyword("");
              setPage(1);
            }}
          />
          <Select
            style={{ width: 130 }}
            value={isActive}
            onChange={(v?: string) => {
              setIsActive(v ?? "active");
              setPage(1);
            }}
            options={[
              { value: "active", label: "启用" },
              { value: "inactive", label: "停用" },
            ]}
          />
        </div>
        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showTotal: (t: number) => `共 ${t} 条`,
            onChange: (p: number, ps: number) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
          locale={{ emptyText: "暂无模板" }}
        />
      </Card>

      <Modal
        title={instantiateTarget ? `从模板实例化：${instantiateTarget.name}` : "创建指标"}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText="实例化创建"
        confirmLoading={loading}
        width={560}
      >
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleCreate} style={{ marginTop: 8 }}>
          {instantiateTarget?.required_fields?.length ? (
            <div style={{ marginBottom: 12 }}>
              <Tag color="orange">本模板必填字段</Tag>
              <span className="muted">{instantiateTarget.required_fields.join("、")}</span>
            </div>
          ) : null}
          <Space style={{ width: "100%" }} wrap>
            <Form.Item
              name="metric_code"
              label="指标编码"
              extra={
                instantiateTarget ? (
                  <span className="muted" style={{ fontSize: 12 }}>
                    已预填模板编码，若与现有指标重复请修改（如加业务后缀）
                  </span>
                ) : (
                  <span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>
                )
              }
              style={{ width: 240 }}
            >
              <Input className="mono" placeholder="留空自动生成" maxLength={64} showCount />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }, { max: 128, message: "名称最长 128 字符" }]} style={{ width: 260 }}>
              <Input maxLength={128} showCount />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]} style={{ width: 240 }}>
              <Cascader
                options={domainOptions}
                placeholder="选择业务域（树形）"
                showSearch
                loading={!domainOptions.length}
                allowClear
                onChange={(v) => void handleInstantiateDomainChange(v ?? [])}
              />
            </Form.Item>
            <Form.Item name="type" label="类型" style={{ width: 240 }}>
              <Select options={["atomic", "derived", "composite"].map((v) => ({ value: v, label: METRIC_TYPE_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="granularity" label="粒度" rules={[{ required: true, message: "请选择粒度" }]} style={{ width: 240 }}>
              <Select
                options={granularityOptions.length ? granularityOptions : undefined}
                showSearch
                placeholder={granularityOptions.length ? "选择粒度" : "输入粒度（字典未加载）"}
                allowClear
              />
            </Form.Item>
            <Form.Item name="unit" label="单位" rules={[{ required: true, message: "请选择单位" }]} style={{ width: 240 }}>
              <Select
                options={unitOptions.length ? unitOptions : undefined}
                showSearch
                placeholder={unitOptions.length ? "选择单位" : "输入单位（字典未加载）"}
                allowClear
              />
            </Form.Item>
            <Form.Item name="aggregation" label="聚合" rules={[{ required: true, message: "请选择聚合方式" }]} style={{ width: 240 }}>
              <Select options={["SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "MAX", "MIN", "MEDIAN", "PERCENTILE"].map((v) => ({ value: v, label: AGGREGATION_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="time_semantics" label="时间语义" rules={[{ required: true, message: "请选择时间语义" }]} style={{ width: 240 }}>
              <Select options={["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"].map((v) => ({ value: v, label: TIME_SEMANTICS_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="freshness" label="新鲜度" rules={[{ required: true, message: "请选择新鲜度" }]} style={{ width: 240 }}>
              <Select options={["REALTIME", "T0", "T1", "HOURLY"].map((v) => ({ value: v, label: FRESHNESS_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="dw_layer" label="数仓层" rules={[{ required: true, message: "请选择数仓层" }]} style={{ width: 240 }}>
              <Select options={["ODS", "DWD", "DWS", "ADS", "DM"].map((v) => ({ value: v, label: DW_LAYER_LABEL[v] ?? v }))} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      {/* 模板详情：描述 / 必填字段 / 默认口径 / 默认属性（数据行已含，无需额外接口） */}
      <Modal
        title={detailTpl ? `模板详情：${detailTpl.name}` : "模板详情"}
        open={!!detailTpl}
        onCancel={() => setDetailTpl(null)}
        footer={<Button onClick={() => setDetailTpl(null)}>关闭</Button>}
        width={620}
        destroyOnHidden
      >
        {detailTpl ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="模板编码"><span className="mono">{detailTpl.code}</span></Descriptions.Item>
              <Descriptions.Item label="业务域"><span className="mono">{detailTpl.domain}</span></Descriptions.Item>
              <Descriptions.Item label="类型">{enumLabel(METRIC_TYPE_LABEL, detailTpl.type) ?? detailTpl.type}</Descriptions.Item>
              <Descriptions.Item label="粒度">{enumLabel(GRANULARITY_LABEL, detailTpl.granularity) ?? detailTpl.granularity}</Descriptions.Item>
              <Descriptions.Item label="聚合">{enumLabel(AGGREGATION_LABEL, detailTpl.aggregation) ?? detailTpl.aggregation}</Descriptions.Item>
              <Descriptions.Item label="数仓层">{enumLabel(DW_LAYER_LABEL, detailTpl.dw_layer) ?? detailTpl.dw_layer}</Descriptions.Item>
              <Descriptions.Item label="新鲜度">{enumLabel(FRESHNESS_LABEL, detailTpl.freshness) ?? detailTpl.freshness}</Descriptions.Item>
              <Descriptions.Item label="状态">{detailTpl.is_active ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>}</Descriptions.Item>
              <Descriptions.Item label="必填字段" span={2}>
                {detailTpl.required_fields?.length ? detailTpl.required_fields.join("、") : <span className="muted">—</span>}
              </Descriptions.Item>
              <Descriptions.Item label="描述" span={2}>
                {detailTpl.description || <span className="muted">—</span>}
              </Descriptions.Item>
            </Descriptions>
            {detailTpl.defaults_json?.definition_json ? (
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>默认口径（实例化时自动合并）</div>
                <pre className="mono" style={{ fontSize: 12, maxHeight: 200, overflow: "auto", background: "#fafafa", padding: 8, borderRadius: 6, margin: 0 }}>
                  {JSON.stringify(detailTpl.defaults_json.definition_json, null, 2)}
                </pre>
              </div>
            ) : null}
          </Space>
        ) : null}
      </Modal>
    </div>
  );
}
