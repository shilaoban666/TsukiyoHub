/// <reference types="vite/client" />

/** 允许 `import xxx from './x.vue'` 的类型声明由 vite/client 提供，这里只补环境变量。 */
interface ImportMetaEnv {
  /** 后端地址覆盖（默认走 vite proxy 到 http://127.0.0.1:8080） */
  readonly VITE_API_BASE?: string
  readonly VITE_APP_TITLE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}