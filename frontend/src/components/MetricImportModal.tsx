import { useState } from "react";
import { Alert, App as AntApp, Button, Modal, Select, Table, Tag, Tooltip, Upload } from "antd";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import {
  downloadMetricImportTemplate,
  importMetricsCsv,
  UnisenseApiError,
  type MetricImportResult,
} from "../api";

export interface MetricImportModalProps {
  open: boolean;
  onClose: () => void;
  /** 目标域下拉选项（value=域 code；label 建议中文名，可含停用标识） */
  domainOptions: Array<{ value: string; label: string }>;
}

const okCount = (r: MetricImportResult) => r.candidates.filter((c) => c.status === "DRAFT").length;
const failCount = (r: MetricImportResult) => r.candidates.length - okCount(r);

/** 批量导入指标弹窗（CSV / Excel xlsx）——指标目录页与注册指标向导页共用。
 *
 * 流程：下载模板（CSV/Excel）→ 选目标域（中文 label）→ 上传文件（逐行容错）→ 逐条结果回显。
 * 域选项由父组件注入，与各自页面的域树/权限语义保持一致。
 */
export default function MetricImportModal({ open, onClose, domainOptions }: MetricImportModalProps) {
  const { message } = AntApp.useApp();
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<MetricImportResult | null>(null);
  const [importDomain, setImportDomain] = useState("");

  function handleUpload(file: File) {
    if (!importDomain) {
      message.warning("请先选择目标域");
      return Upload.LIST_IGNORE;
    }
    const fd = new FormData();
    fd.append("file", file);
    fd.append("domain", importDomain);
    setImporting(true);
    setImportResult(null);
    importMetricsCsv(fd)
      .then((r) => {
        setImportResult(r);
        const ok = okCount(r);
        const fail = failCount(r);
        if (r.row_errors?.length) {
          message.warning(`导入完成：成功 ${ok} 条，失败 ${fail} 条，解析错误 ${r.row_errors.length} 行`);
        } else if (fail > 0) {
          message.warning(`导入完成：成功 ${ok} 条，失败 ${fail} 条（详见下方明细）`);
        } else {
          message.success(`导入完成：成功 ${ok} 条`);
        }
      })
      .catch((e) => {
        message.error(e instanceof UnisenseApiError ? e.message : "批量导入失败");
      })
      .finally(() => setImporting(false));
    return Upload.LIST_IGNORE;
  }

  return (
    <Modal
      title="批量导入指标"
      open={open}
      onCancel={onClose}
      footer={<Button onClick={onClose}>关闭</Button>}
      width={720}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="批量录入存量指标"
        description="上传 CSV 或 Excel（.xlsx）批量创建 DRAFT 指标（编码/名称可缺省，系统自动按域/源表/度量列补全）。外部智能体也可直接调用 POST /api/v1/metric-definitions/batch-import 接口对接（字段说明见 API 文档）。"
      />
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
        <Button
          icon={<DownloadOutlined />}
          onClick={() => downloadMetricImportTemplate("csv").catch(() => message.error("CSV 模板下载失败"))}
        >
          下载 CSV 模板
        </Button>
        <Button
          icon={<DownloadOutlined />}
          onClick={() => downloadMetricImportTemplate("xlsx").catch(() => message.error("Excel 模板下载失败"))}
        >
          下载 Excel 模板
        </Button>
        <Select
          style={{ width: 220 }}
          placeholder="选择目标域"
          value={importDomain || undefined}
          onChange={(v) => setImportDomain(v)}
          options={domainOptions}
          showSearch
          optionFilterProp="label"
        />
      </div>
      <Upload
        accept=".csv,.xlsx"
        showUploadList={false}
        beforeUpload={handleUpload}
      >
        <Button icon={<UploadOutlined />} loading={importing} disabled={!importDomain}>
          选择 CSV / Excel 文件上传
        </Button>
      </Upload>
      {importResult && (
        <>
          <div style={{ borderTop: "1px dashed var(--line)", margin: "14px 0 10px" }} />
          <div className="muted" style={{ marginBottom: 8, fontSize: 12 }}>
            批次 {importResult.batch_id}：成功 {okCount(importResult)} 条，失败 {failCount(importResult)} 条
            {importResult.row_errors?.length ? `，解析错误 ${importResult.row_errors.length} 行` : ""}
          </div>
          {importResult.row_errors && importResult.row_errors.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message="以下行解析失败（未创建）"
              description={
                <ul style={{ maxHeight: 120, overflow: "auto", paddingLeft: 18, margin: 0 }}>
                  {importResult.row_errors.map((r) => (
                    <li key={r.row} className="mono" style={{ fontSize: 12 }}>
                      第 {r.row} 行：{r.error}
                    </li>
                  ))}
                </ul>
              }
            />
          )}
          <Table
            size="small"
            rowKey="metric_code"
            dataSource={importResult.candidates}
            pagination={false}
            columns={[
              { title: "指标编码", dataIndex: "metric_code", ellipsis: true },
              {
                title: "结果",
                dataIndex: "status",
                render: (s: string, r: { validation_errors?: string[] }) =>
                  s === "DRAFT" ? (
                    <Tag color="green">已创建（草稿）</Tag>
                  ) : (
                    <Tooltip title={r.validation_errors?.join("；")}>
                      <Tag color="red">{s}</Tag>
                    </Tooltip>
                  ),
              },
            ]}
          />
        </>
      )}
    </Modal>
  );
}
