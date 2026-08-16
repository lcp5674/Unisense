import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { SchemaTable } from "../components/SchemaTable";
import type { SchemaColumn } from "../types";

// 一个字段缺描述（触发单字段/批量推断按钮），一个已有描述（不触发）
const COLS: SchemaColumn[] = [
  { name: "user_phone", type: "varchar", description: "", comment: "", description_source: null },
  { name: "user_name", type: "varchar", description: "姓名", comment: "姓名", description_source: "manual" },
];

describe("SchemaTable canInfer（LLM 推断按钮权限点）", () => {
  it("canInfer 默认 true 时显示单字段推断与批量推断按钮", () => {
    render(<SchemaTable columns={COLS} inferable onInfer={() => {}} onBatchInfer={() => {}} />);
    expect(screen.getAllByText("推断").length).toBeGreaterThan(0);
    expect(screen.getByText(/批量推断缺失描述（1 个字段）/)).toBeInTheDocument();
  });

  it("canInfer=false 时隐藏单字段推断与批量推断按钮", () => {
    render(<SchemaTable columns={COLS} inferable canInfer={false} onInfer={() => {}} onBatchInfer={() => {}} />);
    expect(screen.queryByText("推断")).not.toBeInTheDocument();
    expect(screen.queryByText(/批量推断缺失描述/)).not.toBeInTheDocument();
  });
});
