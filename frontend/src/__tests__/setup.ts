import "@testing-library/jest-dom";

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

// jsdom 无 getComputedStyle 完整的 offsetWidth 读取，图表可能告警；静默即可
