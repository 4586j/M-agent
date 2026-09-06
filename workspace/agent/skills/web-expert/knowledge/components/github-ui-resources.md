# GitHub 优质 UI 组件素材库

> 按使用场景分类，方便根据需求快速定位合适的组件。
> 收录标准：GitHub 高星 / 社区活跃 / 文档完善 / 可复用性强。
> 更新日期：2026-09-06

---

## 一、综合组件库（按框架分）

### React 组件库

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **shadcn/ui** | https://github.com/shadcn-ui/ui | 可复制粘贴的组件集合，基于 Radix UI + Tailwind，不通过 npm 分发，直接拷代码 | 中后台、Landing Page、任何 React 项目 |
| **Ant Design** | https://github.com/ant-design/ant-design | 企业级 UI 设计语言，组件极其丰富（60+），中文生态最佳 | 中后台管理系统、B 端产品 |
| **Material UI (MUI)** | https://github.com/mui/material-ui | Google Material Design 实现，组件成熟度最高 | 通用 Web 应用、Material 风格产品 |
| **Chakra UI** | https://github.com/chakra-ui/chakra-ui | 模块化、可访问性强、组合式 API | 快速原型、现代风格产品 |
| **NextUI** | https://github.com/nextui-org/nextui | 基于 Tailwind，设计感强，动画流畅 | 现代 Web、设计感强的产品 |
| **Mantine** | https://github.com/mantinedev/mantine | 100+ 组件 + hooks，功能全面 | 中后台、SaaS 产品 |
| **Arco Design** | https://github.com/arco-design/arco-design | 字节跳动出品，企业级，设计细腻 | 中后台、数据密集型产品 |
| **Semi Design** | https://github.com/DouyinDesign/Semi-Design | 抖音设计体系出品，主题定制强 | 中后台、跨端场景 |

### Vue 组件库

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Element Plus** | https://github.com/element-plus/element-plus | Vue 3 版 Element，组件齐全，中文文档完善 | 中后台管理系统 |
| **Vuetify** | https://github.com/vuetifyjs/vuetify | Vue 的 Material Design 实现 | 通用 Web、Material 风格 |
| **Naive UI** | https://github.com/tusen-ai/naive-ui | Vue 3 组件库，TypeScript 友好，主题定制灵活 | 中后台、现代风格产品 |
| **Ant Design Vue** | https://github.com/vueComponent/ant-design-vue | Ant Design 的 Vue 实现 | 中后台、B 端产品 |
| **Nuxt UI** | https://github.com/nuxt/ui | Nuxt 生态的 UI 库，基于 Tailwind + Headless UI | Nuxt 全栈项目 |
| **PrimeVue** | https://github.com/primefaces/primevue | 400+ 主题可选，组件数量极多 | 企业级应用、多主题需求 |

### Angular 组件库

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **NG-ZORRO** | https://github.com/NG-ZORRO/ng-zorro-antd | Ant Design 的 Angular 实现 | 中后台、B 端产品 |
| **Angular Material** | https://github.com/angular/components | Angular 官方 Material 组件 | 通用 Angular 项目 |

### 跨框架 / 原生

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Bootstrap** | https://github.com/twbs/bootstrap | 最经典的 CSS 框架，栅格 + 组件 | 快速原型、传统 Web |
| **Bulma** | https://github.com/jgthms/bulma | 纯 CSS 框架，Flexbox 布局 | 轻量级项目 |
| **DaisyUI** | https://github.com/saadeghi/daisyui | Tailwind CSS 插件式组件库，主题丰富 | Tailwind 项目快速搭建 |

---

## 二、Headless / 无样式组件（可访问性优先）

> 只提供逻辑和可访问性，不绑定样式，适合需要完全自定义视觉的项目。

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Radix UI** | https://github.com/radix-ui/primitives | shadcn/ui 的底层，无样式、可访问性极佳 | 需要完全自定义样式的项目 |
| **React Aria** | https://github.com/adobe/react-spectrum | Adobe 出品，W3C ARIA 最佳实践 | 企业级无障碍需求 |
| **Headless UI** | https://github.com/tailwindlabs/headlessui | Tailwind Labs 出品，与 Tailwind 完美配合 | Tailwind 项目 |
| **Floating UI** | https://github.com/floating-ui/floating-ui | 浮层定位引擎（Tooltip/Popover/Dropdown 底层） | 所有需要浮层定位的组件 |
| **Downshift** | https://github.com/downshift-js/downshift | 自动补全/下拉选择/组合框的 headless 逻辑 | 搜索框、选择器 |
| **Zag** | https://github.com/chakra-ui/zag | 框架无关的 headless 状态机组件 | 跨框架复用逻辑 |
| **Base UI** | https://github.com/mui/base-ui | MUI 团队出品的无样式组件 | 需要 MUI 级可访问性但不想要 Material 样式 |

---

## 三、动画与特效

### 动画库

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Framer Motion (Motion)** | https://github.com/motiondivision/motion | React 动画首选，声明式 API，手势/布局动画 | React 项目的所有动画需求 |
| **GSAP** | https://github.com/greensock/GSAP | 业界最强动画引擎，性能极佳，框架无关 | 复杂时间线动画、滚动驱动动画 |
| **Animate.css** | https://github.com/animate-css/animate.css | CSS 动画预设集，复制类名即用 | 简单入场/出场动画 |
| **Lottie Web** | https://github.com/airbnb/lottie-web | 播放 After Effects 导出的动画（JSON） | 精致微动效、品牌动画 |
| **Motion One** | https://github.com/motiondivision/motion-one | 基于 Web Animations API 的轻量动画库 | 轻量动画需求 |
| **Anime.js** | https://github.com/juliangarnier/anime | 轻量 JS 动画引擎，API 简洁 | 通用 JS 动画 |

### 特效组件集

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Aceternity UI** | https://ui.aceternity.com/ | 炫酷的 React 特效组件（3D、粒子、文字动效等），复制即用 | Landing Page、营销页 |
| **Magic UI** | https://magicui.design/ | 基于 Tailwind + Framer Motion 的 Landing Page 组件 | Landing Page、产品介绍页 |
| **Tilt** | https://github.com/gijsroge/tilt.js | 鼠标跟随 3D 倾斜效果 | 卡片、图片悬停效果 |
| **Vanta.js** | https://github.com/tengbao/vanta | 一键添加 3D 背景动效（波浪、粒子、云等） | 页面背景装饰 |
| **Particles.js / tsparticles** | https://github.com/tsparticles/tsparticles | 粒子背景效果 | 页面背景装饰 |

---

## 四、数据展示

### 表格 / 数据网格

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **TanStack Table** | https://github.com/TanStack/table | Headless 表格逻辑层，框架无关（React/Vue/Solid/Svelte） | 需要完全自定义的表格 |
| **AG Grid** | https://github.com/ag-grid/ag-grid | 功能最全的数据网格（排序/筛选/虚拟/编辑/分组） | 企业级数据密集型应用 |
| **Handsontable** | https://github.com/handsontable/handsontable | 类 Excel 电子表格组件 | 数据编辑、电子表格场景 |
| **TanStack Virtual** | https://github.com/TanStack/virtual | Headless 虚拟滚动，支持任意列表/表格 | 大数据量列表/表格 |

### 图表 / 可视化

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Apache ECharts** | https://github.com/apache/echarts | 百度出品，功能最全的图表库，中文文档优秀 | 各类数据可视化 |
| **Chart.js** | https://github.com/chartjs/Chart.js | 轻量级图表库，上手简单 | 简单图表需求 |
| **D3.js** | https://github.com/d3/d3 | 数据驱动文档，最底层的可视化库，自由度最高 | 高度定制化可视化 |
| **Recharts** | https://github.com/recharts/recharts | React + D3 的声明式图表库 | React 项目图表 |
| **Visx** | https://github.com/airbnb/visx | Airbnb 出品，React 可视化原语集合 | React 项目定制化图表 |
| **AntV** | https://github.com/antvis | 蚂蚁可视化方案集（G2/G6/L7/S2/X6） | 各类数据可视化、图编辑 |
| **Three.js** | https://github.com/mrdoob/three.js | 3D 可视化引擎 | 3D 场景、数据大屏 |
| **React Flow** | https://github.com/xyflow/xyflow | 节点/流程图编辑器 | 工作流、流程图、节点编辑器 |

---

## 五、表单与输入

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **React Hook Form** | https://github.com/react-hook-form/react-hook-form | React 表单首选，性能好、API 简洁 | React 表单 |
| **Formik** | https://github.com/jaredpalmer/formik | React 表单经典方案 | React 表单 |
| **VeeValidate** | https://github.com/logaretm/vee-validate | Vue 表单验证 | Vue 表单 |
| **Yup** | https://github.com/jquense/yup | 对象 schema 验证（常与表单库配合） | 表单数据验证 |
| **Zod** | https://github.com/colinhacks/zod | TypeScript 优先的 schema 验证 | 全栈数据验证 |
| **React Datepicker** | https://github.com/Hacker0x0/react-datepicker | React 日期选择器 | 日期选择 |
| **Flatpickr** | https://github.com/flatpickr/flatpickr | 框架无关的日期/时间选择器 | 通用日期选择 |
| **React Select** | https://github.com/JedWatson/react-select | React 下拉选择器，功能丰富 | 选择器、搜索选择 |
| **Autosuggest** | https://github.com/moroshko/react-autosuggest | 自动补全输入框 | 搜索建议、自动补全 |

---

## 六、反馈与通知

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Sonner** | https://github.com/emilkowalski/sonner | 现代 Toast 库，动画流畅 | 轻提示 |
| **react-hot-toast** | https://github.com/timolins/react-hot-toast | 轻量 React Toast | 轻提示 |
| **react-toastify** | https://github.com/fkhadra/react-toastify | 功能全面的 Toast 库 | 复杂通知场景 |
| **Notistack** | https://github.com/iamhosseindhv/notistack | Material UI 风格的 Snackbar 队列 | MUI 项目通知 |
| **Sweetalert2** | https://github.com/sweetalert2/sweetalert2 | 美观的弹窗/对话框 | 确认弹窗、提示框 |
| **Tippy.js** | https://github.com/atomiks/tippyjs | Tooltip / Popover / Dropdown 一站式方案 | 悬浮提示 |

---

## 七、图标

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Lucide** | https://github.com/lucide-icons/lucide | shadcn/ui 默认图标库，线条风格，1400+ 图标 | 现代 UI 项目 |
| **Heroicons** | https://github.com/tailwindlabs/heroicons | Tailwind Labs 出品，线条+实心双风格 | Tailwind 项目 |
| **Phosphor Icons** | https://github.com/phosphor-icons/phosphor-icons | 6 种风格切换，图标数量多 | 多风格需求 |
| **Tabler Icons** | https://github.com/tabler/tabler-icons | 5000+ 图标，线条风格，覆盖面广 | 需要大量图标 |
| **Font Awesome** | https://github.com/FortAwesome/Font-Awesome | 经典图标库，SVG/字体/CSS 多格式 | 通用项目 |
| **Iconify** | https://github.com/iconify/iconify | 统一图标框架，聚合 200+ 图标集 | 需要混用多个图标集 |
| **React Icons** | https://github.com/react-icons/react-icons | 聚合所有流行图标库的 React 组件 | React 项目 |
| **Animated Heroicons** | https://heroicons-animated.vercel.app/ | 316 个动画版 Heroicons | 需要动效图标 |

---

## 八、富文本编辑器

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **TipTap** | https://github.com/ueberdosis/tiptap | 基于 ProseMirror 的 headless 编辑器框架 | 自定义富文本编辑器 |
| **Novel** | https://github.com/steven-tey/novel | Notion 风格编辑器，AI 自动补全 | AI 写作工具 |
| **Editor.js** | https://github.com/codex-team/editor.js | 块编辑器，输出 JSON 数据 | 结构化内容编辑 |
| **Quill** | https://github.com/slab/quill | 经典开源富文本编辑器 | 通用富文本需求 |
| **Milkdown** | https://github.com/Milkdown/milkdown | Markdown 所见即所得编辑器 | Markdown 编辑场景 |
| **Trix** | https://github.com/basecamp/trix | Basecamp 出品，简洁的富文本编辑器 | 轻量级文本编辑 |

---

## 九、拖拽与排序

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **dnd-kit** | https://github.com/clauderic/dnd-kit | React 拖拽首选，可访问性好、模块化 | React 拖拽排序 |
| **SortableJS** | https://github.com/SortableJS/Sortable | 框架无关的拖拽排序库 | 通用拖拽需求 |
| **React DnD** | https://github.com/react-dnd/react-dnd | React 拖拽底层库，灵活但复杂 | 复杂拖拽交互 |
| **React Beautiful DnD** | https://github.com/atlassian/react-beautiful-dnd | Atlassian 出品，动画流畅（已停维护，推荐 dnd-kit） | 列表拖拽 |
| **Framer Motion LayoutGroup** | 内置于 Framer Motion | 布局动画 + 拖拽 | 简单拖拽 + 动画 |

---

## 十、Landing Page / 营销页专用

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Aceternity UI** | https://ui.aceternity.com/ | 炫酷特效组件集合 | 营销页、产品页 |
| **Magic UI** | https://magicui.design/ | Tailwind + Framer Motion 动效组件 | 产品介绍页 |
| **Origin UI** | https://originui.com/ | 精美 UI 组件集合（Tailwind + Next.js） | 通用页面 |
| **Launch UI** | https://www.launchuicomponents.com/ | Landing Page 专用组件 | 产品发布页 |
| **Kokonut UI** | https://kokonutui.com/ | Next.js 现代组件集 | Next.js 项目 |
| **Re UI** | https://reui.io/ | React 组件 + 动效集合 | 通用页面 |
| **21st.dev** | https://21st.dev/ | shadcn/ui 组件的 npm 式分发平台 | shadcn 生态 |

---

## 十一、设计系统 / 模板

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **shadcn-admin** | https://github.com/satnaing/shadcn-admin | shadcn/ui + Vite 管理后台模板 | 中后台项目起步 |
| **Shadboard** | https://github.com/Qualiora/shadboard | Next.js 15 + Tailwind v4 管理后台 | 管理后台起步 |
| **Untitled UI** | https://www.untitledui.com/ | Figma + React 双端设计系统 | 设计到开发一体化 |
| **Gluestack UI** | https://gluestack.io/ | React + React Native 统一组件库 | 跨端（Web + Mobile） |
| **Functional UI Kit** | https://www.figma.com/community/file/1338456115232271694 | Figma 社区免费设计系统 | 设计参考 |

---

## 十二、加载与骨架屏

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Loading UI** | https://loading-ui.com/ | 精选 spinner / loader 动画集，复制即用 | 加载状态 |
| **DotMatrix** | https://dotmatrix.zzzzshawn.cloud/ | 55+ 开源加载器（React + Tailwind） | 加载状态 |
| **React Content Loader** | https://github.com/danilowoz/react-content-loader | SVG 骨架屏组件 | 内容加载占位 |
| **NProgress** | https://github.com/rstacruz/nprogress | 顶部细进度条 | 页面/路由切换加载指示 |

---

## 十三、滚动与视差

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Lenis** | https://github.com/darkroomengineering/lenis | 现代平滑滚动库 | 全站平滑滚动 |
| **Locomotive Scroll** | https://github.com/locomotivemtl/locomotive-scroll | 滚动驱动动画 + 视差 | 创意网站 |
| **ScrollReveal** | https://github.com/scrollreveal/scrollreveal | 滚动进入动画 | 元素滚动显现 |
| **AOS** | https://github.com/michalsnik/aos | 简单的滚动动画库 | 快速添加滚动动效 |
| **Intersection Observer Polyfill** | https://github.com/w3c/IntersectionObserver | 滚动进入检测的 polyfill | 懒加载、无限滚动 |

---

## 十四、3D / WebGL

| 名称 | 链接 | 说明 | 适用场景 |
|---|---|---|---|
| **Three.js** | https://github.com/mrdoob/three.js | 3D 图形引擎 | 3D 场景、产品展示 |
| **React Three Fiber** | https://github.com/pmndrs/react-three-fiber | Three.js 的 React 渲染器 | React 3D 场景 |
| **Drei** | https://github.com/pmndrs/drei | React Three Fiber 的实用工具集 | R3F 项目辅助 |
| **Spline** | https://spline.design/ | 3D 设计工具 + Web 运行时 | 3D 交互嵌入网页 |
| **Model Viewer** | https://github.com/google/model-viewer | Google 出品，Web 组件嵌入 3D 模型 | 产品 3D 展示 |

---

## 十五、Awesome 清单（综合导航）

> 当需要找某个特定类型的组件时，先查这些 awesome 清单。

| 名称 | 链接 | 说明 |
|---|---|---|
| **awesome-react-components** | https://github.com/brillout/awesome-react-components | React 组件百科全书 |
| **awesome-ui-component-library** | https://github.com/anubhavsrivastava/awesome-ui-component-library | 各框架 UI 库汇总 |
| **awesome-shadcn-ui** | https://github.com/birobirobiro/awesome-shadcn-ui | shadcn/ui 生态大全 |
| **awesome-dataviz** | https://github.com/hal9ai/awesome-dataviz | 数据可视化资源大全 |
| **awesome-icons** | https://github.com/vkarampinis/awesome-icons | 图标库大全 |
| **awesome-wysiwyg-editors** | https://github.com/JefMari/awesome-wysiwyg-editors | 富文本编辑器大全 |
| **awesome-grid** | https://github.com/fancygrid/awesome-grid | 表格/网格库大全 |
| **awesome-css** | https://github.com/uhub/awesome-css | CSS 框架/库大全 |
| **awesome-design-tools** | https://github.com/goabstract/Awesome-Design-Tools | 设计工具 + UI Kit 大全 |
| **awesome.cube.dev** | https://awesome.cube.dev/ | 前端数据工具大全 |

---

## 选型速查

| 需求场景 | 推荐首选 | 备选 |
|---|---|---|
| React 中后台 | Ant Design / Mantine | Arco Design、MUI |
| Vue 中后台 | Element Plus / Naive UI | Ant Design Vue、PrimeVue |
| Landing Page | Aceternity UI + Magic UI | Launch UI、Kokonut UI |
| 自定义设计系统 | Radix UI + Tailwind | Headless UI + Tailwind |
| 数据可视化 | ECharts | AntV、Recharts |
| 复杂表格 | AG Grid | TanStack Table + 自建 |
| 表单 | React Hook Form + Zod | Formik + Yup |
| 动画 | Framer Motion | GSAP |
| 拖拽排序 | dnd-kit | SortableJS |
| 富文本编辑 | TipTap | Novel、Editor.js |
| Toast 通知 | Sonner | react-hot-toast |
| 图标 | Lucide | Phosphor Icons、Tabler Icons |
| 3D 展示 | React Three Fiber + Drei | Spline、Model Viewer |
| 跨端（Web+Mobile） | Gluestack UI | React Native + 自建 |

---

## 变更记录

- 2026-09-06 v1.0：初版，涵盖 15 个分类、100+ 组件库/工具。
