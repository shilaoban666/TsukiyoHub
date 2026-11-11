<script setup lang="ts">
/**
 * 应用外壳：顶栏（品牌 / 导航 / 后端状态 / 主题）+ 内容区。
 *
 * 为什么自绘而不是用 AntDV 的 Layout：工作台是「聊天优先」的单页形态，
 * 不需要后台那套侧边菜单 + 多标签栏；自绘 60 行反而更好控制高度与留白。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  BulbOutlined,
  BulbFilled,
  CloudServerOutlined,
  MessageOutlined,
  BookOutlined,
  ThunderboltOutlined,
  SettingOutlined,
} from '@ant-design/icons-vue'
import { useAppStore } from '@/stores/app'
import { BACKEND_HINT } from '@/api/client'

const app = useAppStore()
const route = useRoute()

const NAV = [
  { name: 'chat', path: '/chat', label: '聊天', icon: MessageOutlined },
  { name: 'knowledge', path: '/knowledge', label: '知识库', icon: BookOutlined },
  { name: 'skills', path: '/skills', label: '技能', icon: ThunderboltOutlined },
  { name: 'settings', path: '/settings', label: '设置', icon: SettingOutlined },
]

/** 后端状态文案 / 颜色 */
const backendText = computed(() => {
  const b = app.backend
  if (b.checking && b.online === null) return '连接中…'
  if (b.online === true) {
    const parts = [b.vectorStore ? `向量：${b.vectorStore}` : '']
    if (b.llmReady === false) parts.push('模型未就绪')
    return parts.filter(Boolean).join(' · ') || '后端已连接'
  }
  if (b.online === false) return '后端未连接'
  return '未检测'
})

const backendClass = computed(() => {
  if (app.backend.online === true) return 'ok'
  if (app.backend.online === false) return 'down'
  return 'idle'
})

const refresh = ref(false)
async function doRefreshHealth() {
  if (refresh.value) return
  refresh.value = true
  try {
    await app.refreshHealth()
  } finally {
    refresh.value = false
  }
}

/** 30s 轮询一次；标签页不可见时跳过，避免无意义请求 */
let timer = 0
onMounted(() => {
  void doRefreshHealth()
  timer = window.setInterval(() => {
    if (document.visibilityState === 'visible') void doRefreshHealth()
  }, 30_000)
})
onBeforeUnmount(() => window.clearInterval(timer))

/** 当前路由名（用于导航高亮；/chat/:id 也算聊天） */
const activeName = computed(() => (route.name as string) || 'chat')
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="logo">月</span>
        <span class="name">TsukiyoHub</span>
        <span class="sub">工作台</span>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in NAV"
          :key="item.name"
          :to="item.path"
          class="nav-item"
          :class="{ active: activeName === item.name }"
        >
          <component :is="item.icon" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="right">
        <button class="status" :class="backendClass" type="button" @click="doRefreshHealth">
          <CloudServerOutlined />
          <span class="status-text">{{ backendText }}</span>
        </button>

        <button class="icon-btn" type="button" :title="app.isDark ? '切换到浅色' : '切换到深色'" @click="app.toggleTheme()">
          <BulbFilled v-if="app.isDark" />
          <BulbOutlined v-else />
        </button>
      </div>
    </header>

    <!-- 后端不可达：明确告诉用户后端地址，而不是让页面静默空白 -->
    <div v-if="app.offline" class="offline-bar">
      <span>后端未连接（{{ BACKEND_HINT }}）——请先启动服务：</span>
      <code>uvicorn tsukiyohub.app:app --port 8080</code>
      <a @click="doRefreshHealth">重试</a>
    </div>

    <main class="content">
      <RouterView v-slot="{ Component }">
        <Transition name="page-fade" mode="out-in">
          <component :is="Component" />
        </Transition>
      </RouterView>
    </main>
  </div>
</template>

<style scoped lang="less">
.shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  background: var(--color-bg-page);
}

.topbar {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  height: var(--header-height);
  flex: 0 0 var(--header-height);
  padding: 0 var(--space-4);
  background: var(--color-bg-card);
  border-bottom: 1px solid var(--color-border-light);
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: var(--color-primary);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
}
.name {
  font-size: var(--fs-sm);
  font-weight: 600;
  letter-spacing: 0.2px;
}
.sub {
  font-size: var(--fs-xs);
  color: var(--color-text-3);
  padding-left: 2px;
}

.nav {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex: 1;
  min-width: 0;
}
.nav-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: var(--radius-sm);
  color: var(--color-text-2);
  font-size: var(--fs-sm);
  transition: background-color var(--dur-fast) var(--ease);

  &:hover {
    background: var(--color-bg-hover);
    color: var(--color-text-1);
  }
  &.active {
    background: var(--color-primary-bg);
    color: var(--color-primary);
    font-weight: 500;
  }
}

.right {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  background: transparent;
  color: var(--color-text-2);
  font-size: var(--fs-xs);
  cursor: pointer;
  max-width: 260px;

  &::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--color-text-3);
    flex: 0 0 auto;
  }
  &.ok::before {
    background: var(--color-success);
  }
  &.down::before {
    background: var(--color-error);
  }
  &.idle::before {
    background: var(--color-warning);
  }
}
.status-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-2);
  cursor: pointer;

  &:hover {
    color: var(--color-primary);
    border-color: var(--color-primary);
  }
}

.offline-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: 6px var(--space-4);
  background: var(--color-error-bg);
  color: var(--color-error);
  font-size: var(--fs-xs);
  border-bottom: 1px solid var(--color-border-light);

  code {
    font-family: var(--font-mono);
    background: var(--color-bg-card);
    padding: 1px 6px;
    border-radius: 4px;
    color: var(--color-text-1);
  }
  a {
    cursor: pointer;
    text-decoration: underline;
  }
}

.content {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

// 窄屏：隐藏副标题与导航文字，只留图标
@media (max-width: 900px) {
  .sub {
    display: none;
  }
  .nav-item span {
    display: none;
  }
  .status-text {
    display: none;
  }
}
</style>