import '@testing-library/jest-dom/vitest'

// jsdom 无 scrollIntoView / matchMedia 等浏览器 API，按需兜底
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView || (() => {})
