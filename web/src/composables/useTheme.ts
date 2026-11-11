/**
 * 主题：浅色 / 深色 / 跟随系统。
 *
 * 两条链路同时维护，保证「CSS 变量」和「Ant Design Vue 组件」颜色一致：
 * 1. `html[data-theme]` → `styles/tokens.less` 里的 CSS 变量（自绘组件用）；
 * 2. `a-config-provider :theme` → AntDV 的 token / 算法（AntDV 组件用）。
 *
 * 从旧前端 `composables/useTheme.ts` 改造：去掉了图表配色（本项目无图表），
 * 主题模式改为从 `stores/app.ts` 持久化读取。
 */
import { computed, watch } from 'vue'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import { theme as antdTheme } from 'ant-design-vue'
import { useAppStore } from '@/stores/app'

// 日期格式化统一中文（会话列表的「昨天 14:03」等）
dayjs.locale('zh-cn')

const LIGHT = {
  colorPrimary: '#0089ff',
  colorSuccess: '#00b578',
  colorWarning: '#ff8f1f',
  colorError: '#ff3141',
  colorInfo: '#0089ff',
  colorTextBase: '#171a1d',
  colorBorder: '#e5e6eb',
  colorBgLayout: '#f5f6f7',
}

const DARK = {
  colorPrimary: '#3aa0ff',
  colorSuccess: '#28c88e',
  colorWarning: '#ffa14a',
  colorError: '#ff5a66',
  colorInfo: '#3aa0ff',
  colorBgLayout: '#141414',
  colorBgContainer: '#1f1f1f',
  colorBgElevated: '#262626',
  colorBorder: '#303133',
  colorTextBase: '#e8e9eb',
}

const COMMON = {
  borderRadius: 6,
  borderRadiusLG: 8,
  fontFamily:
    "-apple-system, 'PingFang SC', 'Microsoft YaHei', 'Helvetica Neue', Arial, sans-serif",
  motionDurationMid: '0.2s',
  motionEaseOut: 'cubic-bezier(0.2, 0, 0, 1)',
}

let mql: MediaQueryList | null = null

export function useThemeSetup() {
  const app = useAppStore()

  // 监听系统主题（只在首次调用时绑定一次）
  if (!mql && typeof window !== 'undefined' && window.matchMedia) {
    mql = window.matchMedia('(prefers-color-scheme: dark)')
    app.setSystemDark(mql.matches)
    mql.addEventListener('change', (e) => {
      app.setSystemDark(e.matches)
    })
  }

  let first = true
  watch(
    () => app.isDark,
    (dark) => {
      const html = document.documentElement
      html.setAttribute('data-theme', dark ? 'dark' : 'light')
      if (!first) {
        // 切主题时给一次过渡，避免生硬闪变
        html.classList.add('theme-transition')
        window.setTimeout(() => html.classList.remove('theme-transition'), 260)
      }
      first = false
    },
    { immediate: true },
  )

  const themeConfig = computed(() => ({
    algorithm: app.isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: { ...COMMON, ...(app.isDark ? DARK : LIGHT) },
  }))

  return { themeConfig }
}