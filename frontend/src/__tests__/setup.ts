import "@testing-library/jest-dom";

// antd/rc-motion 出入场动画与 App.message 通知在 jsdom 中依赖 transitionend/
// requestAnimationFrame 推进，测试在动画完成前结束即触发 React 的 act() 警告
// （"not wrapped in act(...)"，组件为 CSSMotion/ForwardRef/Notifications）。
// 这类警告源自动画生命周期，与组件功能无关，且测试断言仍会捕获真实异步问题
// （若 UI 未按预期更新，断言照样失败，只是少了诊断堆栈）。统一过滤 act 警告
// 以保持测试输出整洁，其余 console.error 原样保留。
//
// 过滤计数 window.__ACT_FILTERED 供测试「正向验证」：断言某操作（如打开/关闭
// 带动画的 Modal）确实产生了被过滤的 act 警告且计数增加，证明过滤机制在工作，
// 而非「无警告=未过滤/过滤坏了」的不可观测状态。
declare global {
  interface Window {
    __ACT_FILTERED?: number;
  }
}
if (typeof window !== "undefined") window.__ACT_FILTERED = 0;

const origConsoleError = console.error;
console.error = (...args: unknown[]) => {
  const text = args
    .map((a) => {
      if (typeof a === "string") return a;
      if (a instanceof Error) return a.message;
      if (a && typeof a === "object") {
        const s = (a as { componentStack?: unknown }).componentStack;
        return typeof s === "string" ? s : "";
      }
      return "";
    })
    .join("\n");
  if (text.includes("not wrapped in act(")) {
    if (typeof window !== "undefined") window.__ACT_FILTERED = (window.__ACT_FILTERED ?? 0) + 1;
    return;
  }
  origConsoleError.apply(console, args as Parameters<typeof console.error>);
};

// antd v5 响应式组件在 jsdom 中依赖 window.matchMedia，测试环境需补齐
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// @ant-design/charts（G2Plot）依赖 ResizeObserver / getComputedStyle 尺寸测量
if (typeof window !== "undefined" && !window.ResizeObserver) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (window as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;
}

// jsdom 未实现 Element.scrollTo，Layout 路由回顶 effect 依赖它
if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => {};
}

// jsdom 无 getComputedStyle 完整的 offsetWidth 读取，图表可能告警；静默即可

// antd Modal/TreeSelect 的 scroll locker 以 getComputedStyle(elt, pseudoElt) 读取尺寸，
// jsdom 只要第二参数存在（含 undefined）就打印 "Not implemented" stderr。丢弃伪元素参数。
if (typeof window !== "undefined") {
  const origGetComputedStyle = window.getComputedStyle.bind(window);
  window.getComputedStyle = (elt: Element, _pseudoElt?: string | null) =>
    origGetComputedStyle(elt);
}
