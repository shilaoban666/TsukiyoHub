<script setup lang="ts">
/**
 * 左侧会话列表：新建 / 重命名 / 删除 / 按时间排序。
 *
 * 数据全部走后端 `/api/sessions`；重命名用「乐观更新 + 失败回滚」，
 * 删除需要二次确认（会话不可恢复）。
 */
import { computed, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  DeleteOutlined,
  EditOutlined,
  MessageOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { useChatStore } from '@/stores/chat'
import { errorText } from '@/api/client'
import { fmtRelative, truncate } from '@/utils/format'
import type { SessionInfo } from '@/types/api'

const chat = useChatStore()

/** 正在重命名的会话 id 与草稿标题 */
const editingId = ref('')
const draft = ref('')
const creating = ref(false)

const list = computed(() => chat.sessions)

async function create() {
  if (creating.value) return
  creating.value = true
  try {
    await chat.createSession('新会话')
    // 新会话没有消息，聚焦到输入框由父组件负责（这里给出提示即可）
  } catch (e) {
    message.error(errorText(e))
  } finally {
    creating.value = false
  }
}

async function startRename(s: SessionInfo) {
  editingId.value = s.id
  draft.value = s.title || ''
}

async function commitRename() {
  const id = editingId.value
  const title = draft.value.trim()
  editingId.value = ''
  if (!id || !title) return
  try {
    await chat.renameSession(id, title)
    message.success('已重命名')
  } catch (e) {
    message.error(`重命名失败：${errorText(e)}`)
  }
}

function cancelRename() {
  editingId.value = ''
  draft.value = ''
}

async function remove(s: SessionInfo) {
  try {
    await chat.removeSession(s.id)
    message.success('会话已删除')
  } catch (e) {
    message.error(`删除失败：${errorText(e)}`)
  }
}

async function refresh() {
  try {
    await chat.loadSessions()
  } catch {
    /* toast 已由请求层弹出 */
  }
}
</script>

<template>
  <aside class="session-list">
    <div class="head">
      <span class="title">会话</span>
      <div class="tools">
        <a-tooltip title="刷新列表">
          <a-button type="text" size="small" :loading="chat.sessionsLoading" @click="refresh">
            <template #icon><ReloadOutlined /></template>
          </a-button>
        </a-tooltip>
        <a-button type="primary" size="small" :loading="creating" @click="create">
          <template #icon><PlusOutlined /></template>
          新建
        </a-button>
      </div>
    </div>

    <div class="scroll">
      <!-- 首次加载 -->
      <div v-if="chat.sessionsLoading && !list.length" class="placeholder">
        <a-skeleton :paragraph="{ rows: 6 }" active />
      </div>

      <!-- 后端不可达 / 加载失败 -->
      <div v-else-if="chat.sessionsError && !list.length" class="placeholder error">
        <p>{{ chat.sessionsError }}</p>
        <a-button size="small" @click="refresh">重试</a-button>
      </div>

      <!-- 空状态 -->
      <div v-else-if="!list.length" class="placeholder empty">
        <MessageOutlined class="empty-icon" />
        <p>还没有会话</p>
        <a-button size="small" type="primary" @click="create">开始第一个对话</a-button>
      </div>

      <!-- 列表 -->
      <ul v-else class="items">
        <li
          v-for="s in list"
          :key="s.id"
          class="item"
          :class="{ active: s.id === chat.activeId }"
          @click="chat.selectSession(s.id)"
        >
          <template v-if="editingId === s.id">
            <a-input
              v-model:value="draft"
              size="small"
              :maxlength="60"
              autofocus
              @click.stop
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="cancelRename"
              @blur="commitRename"
            />
          </template>

          <template v-else>
            <div class="item-main">
              <div class="item-title ellipsis" :title="s.title">{{ s.title || '未命名会话' }}</div>
              <div class="item-sub ellipsis">
                {{ truncate(s.last_message || '暂无消息', 28) }}
              </div>
            </div>
            <div class="item-meta">
              <span class="time">{{ fmtRelative(s.updated_at || s.created_at) }}</span>
              <span class="ops" @click.stop>
                <a-tooltip title="重命名">
                  <a class="op" @click="startRename(s)"><EditOutlined /></a>
                </a-tooltip>
                <a-popconfirm
                  title="删除后不可恢复，确定删除该会话？"
                  ok-text="删除"
                  cancel-text="取消"
                  @confirm="remove(s)"
                >
                  <a class="op danger"><DeleteOutlined /></a>
                </a-popconfirm>
              </span>
            </div>
          </template>
        </li>
      </ul>
    </div>
  </aside>
</template>

<style scoped lang="less">
.session-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  width: var(--session-col);
  flex: 0 0 var(--session-col);
  background: var(--color-bg-card);
  border-right: 1px solid var(--color-border-light);
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-3) var(--space-2);
  border-bottom: 1px solid var(--color-border-light);
}
.title {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--color-text-1);
}
.tools {
  display: flex;
  align-items: center;
  gap: 4px;
}

.scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-2);
}

.placeholder {
  padding: var(--space-4) var(--space-2);
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);

  &.empty {
    padding-top: 48px;
  }
  &.error {
    color: var(--color-error);
  }
  p {
    margin: 0 0 var(--space-2);
  }
}
.empty-icon {
  font-size: 22px;
  margin-bottom: var(--space-2);
  color: var(--color-text-3);
}

.items {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.item {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease);

  &:hover {
    background: var(--color-bg-hover);

    .ops {
      opacity: 1;
    }
  }

  &.active {
    background: var(--color-primary-bg);

    .item-title {
      color: var(--color-primary);
      font-weight: 600;
    }
  }
}

.item-main {
  min-width: 0;
}
.item-title {
  font-size: var(--fs-sm);
  color: var(--color-text-1);
  line-height: 20px;
}
.item-sub {
  font-size: var(--fs-xs);
  color: var(--color-text-3);
  line-height: 16px;
  margin-top: 2px;
}

.item-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 4px;
  min-height: 16px;
}
.time {
  font-size: 11px;
  color: var(--color-text-3);
}
.ops {
  display: flex;
  align-items: center;
  gap: 8px;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease);
}
.op {
  font-size: var(--fs-xs);
  color: var(--color-text-2);

  &:hover {
    color: var(--color-primary);
  }
  &.danger:hover {
    color: var(--color-error);
  }
}
</style>