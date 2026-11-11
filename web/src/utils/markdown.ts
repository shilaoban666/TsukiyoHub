/**
 * Markdown 渲染：`marked` + `DOMPurify` 双保险。
 *
 * 为什么必须保留 DOMPurify：模型输出不可信，可能包含 `<script>` / `onerror=` 等；
 * 前端用 `v-html` 注入 HTML，不过滤就是 XSS 漏洞。
 *
 * 从旧前端 `utils/markdown.ts` 复用（结构与 API 一致），额外补了：
 * - `renderStreaming()`：流式期间只对「已稳定的段落」做 Markdown，尾部保持纯文本，
 *   避免半截 ``` / ** 造成整段闪烁；
 * - `htmlToText()`：复制纯文本用。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { escapeHtml } from './format'

marked.setOptions({ gfm: true, breaks: true })

/** Markdown → 经 DOMPurify 过滤的安全 HTML */
export function renderMarkdown(src: string): string {
  const html = marked.parse(src || '', { async: false }) as string
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } })
}

/**
 * 在已净化的 HTML 的文本节点中高亮关键词（不破坏标签结构）。
 * 用于引用片段里高亮用户问题中的词。
 */
export function highlightHtml(html: string, keyword?: string): string {
  const words = (keyword || '').split(/\s+/).filter(Boolean)
  if (!words.length || typeof DOMParser === 'undefined') return html
  const re = new RegExp(
    `(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`,
    'gi',
  )
  const doc = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html')
  const root = doc.body.firstChild as HTMLElement
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  while (walker.nextNode()) nodes.push(walker.currentNode as Text)
  for (const node of nodes) {
    const text = node.nodeValue || ''
    if (!re.test(text)) continue
    re.lastIndex = 0
    const frag = doc.createDocumentFragment()
    let last = 0
    text.replace(re, (m, _g, offset: number) => {
      frag.appendChild(doc.createTextNode(text.slice(last, offset)))
      const mark = doc.createElement('mark')
      mark.className = 'hl'
      mark.textContent = m
      frag.appendChild(mark)
      last = offset + m.length
      return m
    })
    frag.appendChild(doc.createTextNode(text.slice(last)))
    node.parentNode!.replaceChild(frag, node)
  }
  return root.innerHTML
}

/**
 * 流式专用渲染：把文本切成「稳定前缀 + 未闭合尾部」。
 *
 * 稳定前缀 = 最后一个空行之前的部分（Markdown 的块级边界基本都落在空行上），
 * 尾部用 `<p class="md-tail">` 包纯文本。这样：
 * - 已完成的段落立刻按 Markdown 显示（列表、代码块、表格不闪）；
 * - 尾部不会出现半截语法导致的跳变。
 */
export function renderStreaming(src: string): string {
  const text = src || ''
  if (!text.trim()) return ''

  // 尾部有未闭合的代码块：整体降级成纯文本 + 等宽样式，闭合后自动恢复
  const fenceCount = (text.match(/```/g) || []).length
  if (fenceCount % 2 === 1) {
    return `<pre class="md-streaming-code">${escapeHtml(text)}</pre>`
  }

  const cut = text.lastIndexOf('\n\n')
  if (cut < 0) return `<p class="md-tail">${escapeHtml(text)}</p>`
  const stable = renderMarkdown(text.slice(0, cut))
  const tail = text.slice(cut).replace(/^\n+/, '')
  return `${stable}${tail ? `<p class="md-tail">${escapeHtml(tail)}</p>` : ''}`
}

/** 去掉 Markdown 语法，得到可复制/可预览的纯文本 */
export function htmlToText(html: string): string {
  if (typeof document === 'undefined') return html
  const div = document.createElement('div')
  div.innerHTML = html
  return div.textContent || ''
}