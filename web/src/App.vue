<script setup lang="ts">
/**
 * 应用根组件：只做两件事 —— 注入主题 / 语言，然后渲染外壳。
 * 所有布局逻辑在 `AppShell.vue` 里，保持这里可读。
 */
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import AppShell from '@/components/layout/AppShell.vue'
import { useThemeSetup } from '@/composables/useTheme'

const { themeConfig } = useThemeSetup()
</script>

<template>
  <a-config-provider :theme="themeConfig" :locale="zhCN">
    <AppShell />
  </a-config-provider>
</template>