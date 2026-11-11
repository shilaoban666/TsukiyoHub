/**
 * 应用入口：Pinia + Router + Ant Design Vue + 全局样式。
 *
 * 错误处理策略：
 * - `errorHandler` 里忽略「请求被取消」和业务异常（它们已有 toast），只把真正的渲染错误打到控制台；
 * - `unhandledrejection` 里忽略 AbortError —— 流式请求被用户停止时必然产生，不该刷红。
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'
// 按需注册用到的 Ant Design Vue 组件：全量 `app.use(Antd)` 会打进来 ~1.45MB JS，
// 而工作台只用到下面这 13 个。新增组件时记得在这里补一行。
import {
  Alert,
  Button,
  ConfigProvider,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Radio,
  Select,
  Skeleton,
  Switch,
  Table,
  Tag,
  Tooltip,
} from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'

import App from './App.vue'
import router from './router'
import { isCanceled } from './utils/cancel'

import './styles/tokens.less'
import './styles/global.less'
import './styles/transitions.less'

const app = createApp(App)

const pinia = createPinia()
// persistedstate v4 的 storage 在 store 的 persist 选项里配置（见 stores/app.ts）
pinia.use(piniaPluginPersistedstate)

app.use(pinia)
app.use(router)

for (const plugin of [
  Alert,
  Button,
  ConfigProvider,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Radio,
  Select,
  Skeleton,
  Switch,
  Table,
  Tag,
  Tooltip,
]) {
  app.use(plugin)
}

app.config.errorHandler = (err, _instance, info) => {
  if (isCanceled(err)) return
  console.error('[vue] 渲染异常：', info, err)
}

window.addEventListener('unhandledrejection', (event) => {
  if (isCanceled(event.reason)) {
    event.preventDefault()
    return
  }
  // 其余交给各页面自己的 catch 分支（这里只保证不中断交互）
  console.warn('[未捕获的 Promise 异常]', event.reason)
})

app.mount('#app')