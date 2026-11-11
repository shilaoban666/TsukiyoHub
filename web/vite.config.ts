import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 开发 / 构建配置。
 *
 * 关键点：所有后端请求（含 SSE 流）都走同源 `/api` 前缀，由下面的 proxy 转发到
 * FastAPI（默认 http://127.0.0.1:8080）。这样生产环境只要把静态产物和 API 放在同一个
 * 域名下（或由网关转发 `/api`），前端不需要区分环境即可工作。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_API_PROXY || 'http://127.0.0.1:8080'

  return {
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    css: {
      preprocessorOptions: { less: { javascriptEnabled: true } },
    },
    server: {
      port: 5173,
      host: true,
      proxy: {
        /**
         * SSE 也能穿过 vite 代理。注意后端必须返回 `X-Accel-Buffering: no` 且不压缩，
         * 否则 Nginx / 网关会把流缓冲成「一次性吐出」。
         */
        '/api': { target, changeOrigin: true },
        // 健康检查在根路径下，不在 /api 里，需要单独代理
        '/healthz': { target, changeOrigin: true },
        '/readyz': { target, changeOrigin: true },
      },
    },
    build: {
      target: 'es2020',
      cssCodeSplit: true,
      chunkSizeWarningLimit: 900,
      rollupOptions: {
        output: {
          manualChunks: {
            vendor: ['vue', 'vue-router', 'pinia'],
            antd: ['ant-design-vue', '@ant-design/icons-vue'],
          },
        },
      },
    },
  }
})