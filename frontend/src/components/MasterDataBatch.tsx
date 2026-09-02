import { useMemo, useRef, useState } from "react";
import { Button, Dropdown, Form, Input, message, Modal, Select, Tag } from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  SendOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import type { MenuProps } from "antd";
import { listUsers } from "../api";
import type { BatchResult, CurrentUser, UserBrief } from "../types";

/** 主数据批量治理共享 UI（统一「批量治理」复用模式）。
 *  逻辑度量/维度/术语三页复用：多选后「批量操作」下拉（提交审核/通过/驳回/废弃/直发发布）
 *  + 确认弹窗（明确数量、危险操作红标）+ 评审指派（指定用户/域评审组选项框）
 *  + 失败明细弹窗（超 3 条完整展示）+ 重试失败项。对齐指标 MetricCatalog 完整批量模式
 *  TD §13：逐条收集结果不整体失败。 */

export type BatchActionKey = "submit" | "approve" | "reject" | "deprecate" | "reactivate" | "delete" | "publish";

export interface BatchActionConfig {
  key: BatchActionKey;
  label: string;
  danger?: boolean;
  /** 仅平台管理员（admin 直发发布，如术语 batch-publish） */
  adminOnly?: boolean;
}

/** 评审指派选项：批量提交审核弹窗用 */
export interface BatchReviewerOptions {
  reviewerDomainOptions?: { value: string; label: string }[];
}

interface MasterDataBatchProps<T extends object> extends BatchReviewerOptions {
  /** 选中行（含编码字段 + 状态字段） */
  selected: T[];
  /** 编码字段名（measure_code / dim_code / term_code） */
  codeKey: string;
  /** 状态字段名（默认 status） */
  statusKey?: string;
  /** 实体中文名（"逻辑度量"/"维度"/"术语"，用于文案） */
  entityLabel: string;
  /** 批量操作配置（按模块裁剪） */
  actions: BatchActionConfig[];
  /** 操作可用权限判断（返回 false 禁用对应菜单项） */
  canRun?: (action: BatchActionKey) => boolean;
  /** 执行回调：返回统一 BatchResult（results[].code） */
  onRun: (
    action: BatchActionKey,
    opts: {
      codes: string[];
      reason?: string;
      changeReason?: string;
      reviewerType?: "user" | "domain" | null;
      reviewerId?: number | null;
      reviewerDomain?: string | null;
    },
  ) => Promise<BatchResult>;
  /** 完成回调（刷新列表） */
  onDone?: () => void;
  /** 是否平台管理员（决定 admin 直发发布可见性） */
  isAdmin?: boolean;
  /** 当前用户：评审候选按角色过滤（平台管理员全量；域管理员/评审员限自己域） */
  user?: CurrentUser | null;
  /** 批量按钮文案（默认「批量操作」） */
  buttonLabel?: string;
}

const ACTION_VERB: Record<BatchActionKey, string> = {
  submit: "提交审核",
  approve: "通过",
  reject: "驳回",
  deprecate: "废弃",
  reactivate: "重新启用",
  delete: "删除",
  publish: "发布",
};

export function MasterDataBatch<T extends object>(props: MasterDataBatchProps<T>) {
  const {
    selected,
    codeKey,
    statusKey = "status",
    entityLabel,
    actions,
    canRun,
    onRun,
    onDone,
    reviewerDomainOptions,
    isAdmin = false,
    user,
    buttonLabel = "批量操作",
  } = props;
  const [action, setAction] = useState<BatchActionKey | null>(null);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [errorsOpen, setErrorsOpen] = useState(false);
  const [failedCodes, setFailedCodes] = useState<string[]>([]);
  const [retryCodes, setRetryCodes] = useState<string[] | null>(null);
  const retryActionRef = useRef<BatchActionKey | null>(null);
  const [form] = Form.useForm();
  const [userOptions, setUserOptions] = useState<{ value: number; label: string }[]>([]);

  // 评审用户候选（全局用户列表，打开提交审核弹窗时懒加载，失败静默回退空列表）
  // 按角色过滤：仅 domain_admin / reviewer 有审核权，不展示普通用户（避免指派无人可审）
  const openSubmitModal = (act: BatchActionKey) => {
    setAction(act);
    form.resetFields();
    if (act === "submit" && !userOptions.length) {
      listUsers()
        .then((users: UserBrief[]) =>
          setUserOptions(
            users
              .filter((u) => u.role === "domain_admin" || u.role === "reviewer")
              .map((u) => ({
                value: u.id,
                label: u.display_name ? `${u.display_name}（${u.username}）` : u.username,
              })),
          ),
        )
        .catch(() => setUserOptions([]));
    }
  };
  // 评审域候选：平台管理员可全量；域管理员/评审员仅自己域（防止指派到无权管辖的域）
  const effectiveReviewerDomains = useMemo(() => {
    if (!reviewerDomainOptions) return undefined;
    if (!user || user.role === "platform_admin" || !user.domain) return reviewerDomainOptions;
    return reviewerDomainOptions.filter((d) => d.value === user.domain);
  }, [reviewerDomainOptions, user]);

  const asRec = (row: T) => row as Record<string, unknown>;
  const codesOf = (filter: (s: string) => boolean) =>
    selected.filter((row) => filter(String(asRec(row)[statusKey] ?? ""))).map((row) => String(asRec(row)[codeKey]));

  async function runBatch(act: BatchActionKey) {
    // 重试失败项：仅针对上次失败编码（不重新全量执行）
    let targets: string[] = retryCodes ?? [];
    if (!targets.length) {
      if (act === "submit") targets = codesOf((s) => s === "DRAFT");
      else if (act === "approve") targets = codesOf((s) => s === "REVIEW");
      else if (act === "reject") targets = codesOf((s) => s === "REVIEW");
      else if (act === "reactivate") targets = codesOf((s) => s === "DEPRECATED");
      else if (act === "delete") targets = codesOf((s) => s === "DRAFT" || s === "DEPRECATED");
      else if (act === "publish") targets = codesOf((s) => s === "DRAFT" || s === "DEPRECATED" || s === "PUBLISHED");
      else targets = codesOf((s) => s === "PUBLISHED"); // deprecate
    }
    if (!targets.length) {
      const hint = act === "submit" ? "草稿" : act === "approve" || act === "reject" ? "审核中（REVIEW）" : act === "reactivate" ? "已废弃" : act === "delete" ? "草稿或已废弃" : act === "deprecate" ? "已发布" : "可发布";
      message.warning(`勾选的${entityLabel}中没有${hint}状态可操作`);
      return;
    }

    // 校验评审指派（批量提交审核）：指定用户但未选用户
    const values = form.getFieldsValue();
    if (act === "submit" && values.reviewer_type === "user" && !values.reviewer_id) {
      message.warning("已选择「指定评审用户」，请先选择具体评审人");
      return;
    }

    retryActionRef.current = act;
    setBusy(true);
    try {
      const res = await onRun(act, {
        codes: targets,
        reason: values.reason,
        changeReason: values.change_reason,
        reviewerType: values.reviewer_type ?? null,
        reviewerId: values.reviewer_id ?? null,
        reviewerDomain: values.reviewer_domain ?? null,
      });
      const failed = res.results.filter((r) => !r.ok);
      const failedList = failed.map((r) => r.code);
      setFailedCodes(failedList);
      if (res.ok_count) message.success(`${ACTION_VERB[act]}成功 ${res.ok_count} 个`);
      if (failed.length) {
        setErrors(failed.map((r) => `${r.code}: ${r.message}`));
        if (failed.length <= 3) {
          message.error(failed.map((r) => `${r.code}: ${r.message}`).join("；"));
        } else {
          message.error(`批量操作失败 ${failed.length} 条（前 3 条：${failed.slice(0, 3).map((r) => `${r.code}: ${r.message}`).join("；")}…），点击「查看失败明细」查看全部`);
          setErrorsOpen(true);
        }
      }
      onDone?.();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "批量操作失败");
      setErrors([err instanceof Error ? err.message : "批量操作失败"]);
      setErrorsOpen(true);
    } finally {
      setBusy(false);
      setAction(null);
      setRetryCodes(null);
      form.resetFields();
    }
  }

  const menuItems: MenuProps["items"] = actions
    .filter((a) => !a.adminOnly || isAdmin)
    .filter((a) => (canRun ? canRun(a.key) : true))
    .map((a) => ({
      key: a.key,
      label: a.label,
      danger: a.danger,
      icon: a.key === "approve" ? <CheckOutlined /> : a.key === "reject" ? <CloseOutlined /> : a.key === "submit" ? <SendOutlined /> : undefined,
    }));

  const hasAny = menuItems.length > 0;

  return (
    <>
      <Dropdown
        trigger={["click"]}
        menu={{
          items: menuItems,
          onClick: ({ key }) => openSubmitModal(key as BatchActionKey),
        }}
        disabled={!selected.length || !hasAny}
      >
        <Button icon={<ThunderboltOutlined />} disabled={!selected.length || !hasAny}>
          {buttonLabel}
          {selected.length > 0 && (
            <Tag style={{ marginInlineStart: 4 }}>{selected.length}</Tag>
          )}
        </Button>
      </Dropdown>

      {/* 批量操作确认弹窗 */}
      <Modal
        title={`批量${ACTION_VERB[action ?? "submit"]}${entityLabel}`}
        open={action !== null}
        onCancel={() => setAction(null)}
        onOk={() => runBatch(action!)}
        confirmLoading={busy}
        okText={action ? ACTION_VERB[action] : "确定"}
        okButtonProps={{ danger: action === "deprecate" || action === "delete" || action === "reject" }}
        width={520}
        destroyOnClose
      >
        <p>
          确定批量{ACTION_VERB[action ?? "submit"]}选中的 <b>{retryCodes?.length ?? selected.length}</b> 个{entityLabel}吗？
          {action === "deprecate" ? " 废弃后不可恢复（被下游引用者会被保护拦截）。" : ""}
          {action === "delete" ? " 删除后进入回收站，可恢复（仅草稿/废弃可删；被下游引用者会被保护拦截）。" : ""}
          {action === "reactivate" ? " 重新启用后回到草稿状态，需重新提交审核后才能发布。" : ""}
          {action === "reject" ? " 驳回后将回到草稿状态，需修改后重新提交。" : ""}
          {action === "publish" ? " 直发发布将跳过审核流程（仅平台管理员）。" : ""}
        </p>
        {action === "submit" && (
          <Form form={form} layout="vertical">
            <Form.Item
              name="change_reason"
              label="提交说明"
              initialValue={`批量提交${entityLabel}审核`}
              rules={[{ required: true, min: 4, message: "请填写提交说明（至少 4 字）" }]}
            >
              <Input.TextArea rows={2} maxLength={200} />
            </Form.Item>
            <Form.Item name="reviewer_type" label="评审指派（可选）" extra="不指定则由域管理员兜底评审">
              <Select showSearch
                allowClear
                placeholder="不指派（域管理员兜底）"
                options={[
                  { value: "user", label: "指定用户" },
                  { value: "domain", label: "指定域评审组" },
                ]}
              />
            </Form.Item>
            <Form.Item noStyle shouldUpdate={(prev, cur) => prev.reviewer_type !== cur.reviewer_type}>
              {({ getFieldValue }) =>
                getFieldValue("reviewer_type") === "user" ? (
                  <Form.Item name="reviewer_id" label="评审用户" rules={[{ required: true, message: "请选择评审用户" }]}>
                    <Select
                      showSearch
                      allowClear
                      optionFilterProp="label"
                      placeholder="选择评审用户"
                      options={userOptions}
                      notFoundContent={userOptions.length ? undefined : "暂无可用用户"}
                    />
                  </Form.Item>
                ) : getFieldValue("reviewer_type") === "domain" ? (
                  <Form.Item name="reviewer_domain" label="评审域" rules={[{ required: true, message: "请选择评审域" }]}>
                    <Select
                      showSearch
                      allowClear
                      optionFilterProp="label"
                      placeholder="选择评审域"
                      options={effectiveReviewerDomains}
                      notFoundContent={
                        effectiveReviewerDomains?.length
                          ? undefined
                          : user?.role === "domain_admin" || user?.role === "reviewer"
                            ? "当前域无可指派的评审域"
                            : "暂无启用中的主题域"
                      }
                    />
                  </Form.Item>
                ) : null
              }
            </Form.Item>
          </Form>
        )}
        {action === "reject" && (
          <Form form={form} layout="vertical">
            <Form.Item name="reason" label="驳回原因" rules={[{ required: true, min: 4, message: "请填写驳回原因（至少 4 字）" }]}>
              <Input.TextArea rows={3} maxLength={500} placeholder={`如：定义与业务实际不符，请补充说明后重新提交`} />
            </Form.Item>
          </Form>
        )}
      </Modal>

      {/* 批量操作失败明细弹窗：完整展示所有失败项 + 一键重试失败项 */}
      <Modal
        title="批量操作失败明细"
        open={errorsOpen}
        onCancel={() => setErrorsOpen(false)}
        footer={[
          <Button key="retry" type="primary" disabled={!failedCodes.length} onClick={() => { setErrorsOpen(false); setRetryCodes(failedCodes); openSubmitModal(retryActionRef.current ?? "submit"); }}>
            重试失败项
          </Button>,
          <Button key="close" onClick={() => setErrorsOpen(false)}>关闭</Button>,
        ]}
        width={560}
      >
        <p style={{ marginBottom: 8 }}>共 {errors.length} 条失败：</p>
        <div style={{ maxHeight: 320, overflow: "auto", background: "var(--bg-2)", borderRadius: 6, padding: 8 }}>
          {errors.map((e, i) => (
            <div key={i} style={{ padding: "4px 0", borderBottom: i < errors.length - 1 ? "1px solid var(--border-color)" : undefined }}>
              {e}
            </div>
          ))}
        </div>
      </Modal>
    </>
  );
}
