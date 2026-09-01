import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ConfigProvider, App as AntApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import App from "./App";
import { antdTheme } from "./theme";
import "./styles.css";

// dayjs 与 ConfigProvider 保持同一 locale：antd 的 ConfigProvider locale 只管组件文案，
// DatePicker/RangePicker 日历面板的月份标题由 dayjs 生成，不设 zh-cn 会显示英文（Sep/Oct）。
dayjs.locale("zh-cn");

const root = document.getElementById("root");
if (!root) throw new Error("root element not found");

createRoot(root).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={antdTheme}>
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
);
