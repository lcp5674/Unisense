import { useEffect, useState } from "react";
import { Alert, Button, Form, Input, Modal, Select, Tooltip } from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  SendOutlined,
} from "@ant-design/icons";
import type { CurrentUser, UserBrief } from "../types";
import { listUsers } from "../api";
import type { ReviewSubmitBody } from "../api";

/** 主数据审核流共享 UI（统一「主数据审核」复用模式）。
 *  逻辑度量/维度/术语三类主数据页复用：评审权判断 + 提交审核/通过/驳回按钮 + 提交/驳回 Modal，
 *  避免三套重复代码。对齐指标审核流 TD §13（指派评审人 / 自审禁止 / 驳回原因必填）。 */

/** 审核中状态的标签样式（各页 STATUS_COLOR/STATUS_LABEL 复用） */
export const REVIEW_TAG = { color: "processing", label: "审核中" } as const;

/** 评审可操作的最小行结构（实体行含审核字段即可） */
export interface ReviewRow {
  code: string;
  name: string;
  status: string;
  reviewer_type?: string | null;
  reviewer_id?: number | null;
  reviewer_domain?: string | null;
}

/** 审核权判断（对齐后端 _assert_reviewer_authorized）：指派评审人/域评审组/未指派域管理员兜底 */
export function canReviewMasterData(row: ReviewRow, user: CurrentUser | null): boolean {
  if (!user) return false;
  if (user.role === "platform_admin") return true;
  if (row.reviewer_type === "user" && row.reviewer_id != null) {
    return user.id === row.reviewer_id;
  }
  if (row.reviewer_type === "domain" && row.reviewer_domain) {
    return (
      (user.role === "domain_admin" || user.role === "reviewer") &&
      user.domain === row.reviewer_domain
    );
  }
  return user.role === "domain_admin";
}

/** 提交审核 / 通过 / 驳回 按钮组（行内操作列复用） */
export function MasterDataReviewActions(props: {
  row: ReviewRow;
  user: CurrentUser | null;
  busyCode: string | null;
  /** 提交审核提示文案（tooltip） */
  submitTip?: string;
  /** 审核通过提示文案 */
  approveTip?: string;
  onApprove: (row: ReviewRow) => void;
  onOpenSubmit: (row: ReviewRow) => void;
  onOpenReject: (row: ReviewRow) => void;
}) {
  const { row, user, busyCode, submitTip, approveTip, onApprove, onOpenSubmit, onOpenReject } = props;
  const canReview = canReviewMasterData(row, user);
  return (
    <>
      {row.status === "DRAFT" && (
        <Tooltip title={submitTip ?? "提交审核（发布前须评审通过）"}>
          <Button
            size="small"
            type="primary"
            icon={<SendOutlined />}
            onClick={() => onOpenSubmit(row)}
          >
            提交审核
          </Button>
        </Tooltip>
      )}
      {row.status === "REVIEW" && canReview && (
        <>
          <Tooltip title={approveTip ?? "审核通过并发布"}>
            <Button
              size="small"
              type="primary"
              icon={<CheckOutlined />}
              aria-label="审核通过并发布"
              loading={busyCode === row.code}
              onClick={() => onApprove(row)}
            />
          </Tooltip>
          <Tooltip title="驳回（须填原因）">
            <Button
              size="small"
              danger
              icon={<CloseOutlined />}
              aria-label="驳回该主数据"
              onClick={() => onOpenReject(row)}
            />
          </Tooltip>
        </>
      )}
    </>
  );
}

/** 提交审核 Modal + 驳回审核 Modal（自含表单，destroyOnClose 每次打开重置） */
export function MasterDataReviewModals(props: {
  /** 实体名（"逻辑度量"/"维度"/"术语"，用于标题/文案） */
  entityLabel: string;
  /** 提交审核弹窗的说明描述 */
  submitDescription: string;
  /** 评审指派可选业务域（评审域选择框；不传则回退为手动输入 code） */
  reviewerDomainOptions?: { value: string; label: string }[];
  submitTarget: { code: string; name: string } | null;
  submitBusy: boolean;
  onCancelSubmit: () => void;
  onConfirmSubmit: (values: ReviewSubmitBody) => Promise<void>;
  rejectTarget: { code: string; name: string } | null;
  rejectBusy: boolean;
  onCancelReject: () => void;
  onConfirmReject: (reason: string) => Promise<void>;
}) {
  const {
    entityLabel,
    submitDescription,
    reviewerDomainOptions,
    submitTarget,
    submitBusy,
    onCancelSubmit,
    onConfirmSubmit,
    rejectTarget,
    rejectBusy,
    onCancelReject,
    onConfirmReject,
  } = props;
  const [submitForm] = Form.useForm();
  const [rejectForm] = Form.useForm();
  // 评审用户候选（全局用户列表，三页共用；打开提交弹窗时懒加载，失败静默回退空列表）
  const [userOptions, setUserOptions] = useState<{ value: number; label: string }[]>([]);
  useEffect(() => {
    if (!submitTarget) return;
    listUsers()
      .then((users: UserBrief[]) =>
        setUserOptions(
          users.map((u) => ({
            value: u.id,
            label: `${u.display_name || u.username}（#${u.id}）`,
          })),
        ),
      )
      .catch(() => setUserOptions([]));
  }, [submitTarget]);

  async function handleSubmitOk() {
    const values = await submitForm.validateFields();
    await onConfirmSubmit({
      change_reason: values.change_reason,
      reviewer_type: values.reviewer_type ?? null,
      reviewer_id: values.reviewer_id ?? null,
      reviewer_domain: values.reviewer_domain ?? null,
    });
  }

  async function handleRejectOk() {
    const values = await rejectForm.validateFields();
    await onConfirmReject(values.reason);
  }

  return (
    <>
      {/* 提交审核 Modal（DRAFT → REVIEW）：发布前须评审通过 */}
      <Modal
        title={submitTarget ? `提交审核 · ${submitTarget.name}` : "提交审核"}
        open={!!submitTarget}
        onOk={handleSubmitOk}
        onCancel={onCancelSubmit}
        confirmLoading={submitBusy}
        width={560}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={`${entityLabel}发布前须先评审通过`}
          description={submitDescription}
        />
        <Form form={submitForm} layout="vertical">
          <Form.Item
            name="change_reason"
            label="提交说明"
            rules={[{ required: true, min: 4, message: "请填写提交说明（至少 4 字），说明为何发布" }]}
          >
            <Input.TextArea
              rows={2}
              maxLength={200}
              placeholder={`如：${entityLabel}定义已与业务对齐口径，申请发布`}
            />
          </Form.Item>
          <Form.Item
            name="reviewer_type"
            label="评审指派（可选）"
            extra="不指定则由域管理员兜底评审"
          >
            <Select
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
                <Form.Item
                  name="reviewer_id"
                  label="评审用户"
                  rules={[{ required: true, message: "请选择评审用户" }]}
                >
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
                <Form.Item
                  name="reviewer_domain"
                  label="评审域"
                  rules={[{ required: true, message: "请选择评审域" }]}
                >
                  {reviewerDomainOptions ? (
                    <Select
                      showSearch
                      allowClear
                      optionFilterProp="label"
                      placeholder="选择评审域"
                      options={reviewerDomainOptions}
                      notFoundContent={
                        reviewerDomainOptions.length ? undefined : "暂无启用中的主题域"
                      }
                    />
                  ) : (
                    <Input placeholder="如 outpatient" />
                  )}
                </Form.Item>
              ) : null
            }
          </Form.Item>
        </Form>
      </Modal>

      {/* 驳回审核 Modal（REVIEW → DRAFT）：驳回原因必填，通知提交人修改 */}
      <Modal
        title={rejectTarget ? `驳回审核 · ${rejectTarget.name}` : "驳回审核"}
        open={!!rejectTarget}
        onOk={handleRejectOk}
        onCancel={onCancelReject}
        confirmLoading={rejectBusy}
        width={520}
        destroyOnClose
      >
        <Form form={rejectForm} layout="vertical">
          <Form.Item
            name="reason"
            label="驳回原因"
            rules={[{ required: true, min: 4, message: "请填写驳回原因（至少 4 字），通知提交人修改" }]}
          >
            <Input.TextArea
              rows={3}
              maxLength={500}
              placeholder={`如：定义与业务实际不符，请补充说明后重新提交`}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

// 供页面使用的便捷 hook：管理提交/驳回 target 与 busy 状态
export function useMasterDataReview() {
  const [submitTarget, setSubmitTarget] = useState<{ code: string; name: string } | null>(null);
  const [submitBusy, setSubmitBusy] = useState(false);
  const [rejectTarget, setRejectTarget] = useState<{ code: string; name: string } | null>(null);
  const [rejectBusy, setRejectBusy] = useState(false);
  const [busyCode, setBusyCode] = useState<string | null>(null);

  return {
    submitTarget,
    setSubmitTarget,
    submitBusy,
    setSubmitBusy,
    rejectTarget,
    setRejectTarget,
    rejectBusy,
    setRejectBusy,
    busyCode,
    setBusyCode,
  };
}
