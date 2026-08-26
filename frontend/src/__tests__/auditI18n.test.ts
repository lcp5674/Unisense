import { describe, it, expect } from "vitest";
import { AUDIT_VERB_LABEL } from "../utils/auditI18n";

// P2-1（第八轮）：后端审计 action 的动词兜底表——此前缺失 12 个动词导致前端
// 直接显示英文原码（后端已 enrich action_desc 故仅兜底，但缺项仍体验割裂）。
// 本测试锚定这 12 个动词已补齐，防止未来再次漏加。
describe("AUDIT_VERB_LABEL 动词兜底表", () => {
  it("补齐第八轮审查发现的 12 个缺失动词", () => {
    const required: Record<string, string> = {
      clarify: "提交澄清",
      purge: "清除",
      reactivate: "恢复",
      cancel_job: "取消任务",
      list_databases: "查询数据库列表",
      list_tables: "查询表列表",
      refine: "精炼",
      delete_all: "清空",
      preview_values: "预览枚举值",
      batch_create: "批量创建",
      sql_batch_parse: "SQL 批量解析",
      sql_batch_register: "SQL 批量注册",
    };
    for (const [verb, label] of Object.entries(required)) {
      expect(AUDIT_VERB_LABEL[verb], `动词 ${verb} 应有中文标签`).toBe(label);
    }
  });
});
