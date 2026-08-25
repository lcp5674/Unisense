// 全局搜索条目 → 目标路由跳转（Layout 顶栏下拉与 /search 页共用）
import type { NavigateFunction } from "react-router-dom";
import type { GlobalSearchItem } from "../types";

/**
 * 按搜索结果类型跳转到对应详情/列表页。
 *
 * 列表页（维度/术语/模板/数据源/采集目录）统一带 `?kw=` 定位，
 * 术语额外带 `?focus=` 行高亮；指标跳详情页；主题域跳管理页。
 */
export function navigateToSearchItem(
  navigate: NavigateFunction,
  item: GlobalSearchItem,
  fallbackQuery: string,
): void {
  switch (item.type) {
    case "metric":
      navigate(`/detail/${encodeURIComponent(item.code)}`);
      break;
    case "dimension":
      navigate(`/dimensions?kw=${encodeURIComponent(item.code)}`);
      break;
    case "term":
      navigate(`/glossary?kw=${encodeURIComponent(item.code)}&focus=${encodeURIComponent(item.code)}`);
      break;
    case "template":
      navigate(`/templates?kw=${encodeURIComponent(item.name)}`);
      break;
    case "data_source":
      navigate(`/data-sources?kw=${encodeURIComponent(item.code)}`);
      break;
    case "catalog":
    case "field":
      navigate(`/catalogs?kw=${encodeURIComponent(item.code)}`);
      break;
    case "measure":
      navigate(`/measure-catalogs?kw=${encodeURIComponent(item.name)}`);
      break;
    case "subject_domain":
      navigate("/domains");
      break;
    default:
      navigate(`/search?q=${encodeURIComponent(fallbackQuery)}`);
  }
}
