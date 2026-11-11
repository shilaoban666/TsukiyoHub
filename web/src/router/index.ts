/**
 * 路由表：4 条扁平路由，没有权限系统。
 *
 * `/chat/:sessionId?` 用可选参数表示「当前选中的会话」，这样刷新页面 / 分享链接都能回到同一个会话。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  {
    path: '/chat/:sessionId?',
    name: 'chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '聊天' },
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/KnowledgeView.vue'),
    meta: { title: '知识库' },
  },
  {
    path: '/skills',
    name: 'skills',
    component: () => import('@/views/SkillsView.vue'),
    meta: { title: '技能' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach((to) => {
  const title = (to.meta.title as string) || ''
  document.title = title ? `${title} · TsukiyoHub 工作台` : 'TsukiyoHub 工作台'
  return true
})

// 发版后旧 chunk 404：刷新一次拿到新版本（只刷一次，避免死循环）
router.onError((err) => {
  const msg = String((err as Error)?.message || '')
  if (/Failed to fetch dynamically imported module|Importing a module script failed/i.test(msg)) {
    const KEY = 'tsukiyohub-chunk-reload'
    if (!sessionStorage.getItem(KEY)) {
      sessionStorage.setItem(KEY, '1')
      window.location.reload()
    }
  }
})

export default router