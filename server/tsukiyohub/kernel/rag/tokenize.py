"""中文分词：jieba（懒加载 + 预热）+ 字符二元组兜底。

为什么要做两层
--------------
BM25 是**词袋**模型，中文没有空格，必须先分词。两种方案的权衡：

============  ========  ================================================
方案          质量      依赖
============  ========  ================================================
jieba         好        需安装；首次 ``initialize()`` 要扫词典（约 0.5~1.5 秒）
字符二元组    够用      零依赖，``年假怎么算`` → ``年假/假怎/怎么/么算``
============  ========  ================================================

字符二元组是中文检索里被验证过的实用方案：天然覆盖所有二字词，不需要词典。
所以这里的策略是 **jieba 可用就用、装不上自动降级**，保证「检索永远可用」。

为什么要预热
------------
jieba 第一次分词会构建前缀词典（纯 Python，几百毫秒到 1.5 秒）。如果发生在
第一个用户请求里，会表现为「首问特别慢」。:func:`prewarm` 在后台线程里提前
``initialize()``，把这段延迟挪到启动期；显式调用一次即可，失败完全无害。

索引期与查询期**必须**用同一个 :func:`tokenize`，否则倒排对不上——这是本模块
只暴露一个分词入口的原因。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from functools import lru_cache

from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: 英文 / 数字 / 编号串（含小数、连字符、下划线，覆盖 OA-2024-017、v2.1、8:30 等）
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._\-:/][a-z0-9]+)*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

#: 中文停用词：手册类文本高频虚词，留着会拉低 BM25 区分度
STOPWORDS: frozenset[str] = frozenset(
    """的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这
    我们 可以 这个 那个 什么 怎么 如何 以及 但是 因为 所以 如果 或者 并且 对于 关于 根据 按照
    请 需要 应当 应该 必须 不得 相关 进行 通过 由于 为了 以下 如下 其他 时候 情况 问题
    一下 是否 能否 多少 哪些 哪个 哪里 为什么 可以吗 行吗 吗 呢 啊 吧 呀
    a an the of to in on for and or is are be as at by with""".split()
)

#: 英文停用词（与中文合并比较，统一小写）
_ASCII_STOPWORDS: frozenset[str] = frozenset(
    "a an the of to in on for and or is are be as at by with it this that".split()
)

_PREWARM_LOCK = threading.Lock()
_PREWARMED = False


@lru_cache(maxsize=1)
def jieba_available() -> bool:
    """jieba 是否可用（只探测一次）。"""
    try:
        import jieba  # noqa: F401
    except ImportError:
        return False
    return True


def tokenizer_name() -> str:
    """当前生效的分词器名称，用于自检与健康检查。"""
    return "jieba" if jieba_available() else "bigram"


@lru_cache(maxsize=1)
def _load_jieba():
    """延迟初始化 jieba，并抑制其初始化日志（只做一次）。

    ``setLogLevel`` 会抢日志模块的全局锁：每切一次词都调用时，20 并发检索的 L1 重排
    会从 0.2 秒涨到十几秒（源项目压测定位过），所以只在这里调一次。
    """
    import jieba

    jieba.setLogLevel(60)  # 关掉 "Building prefix dict ..." 之类输出
    return jieba


def prewarm(background: bool = True) -> None:
    """预热 jieba 词典（幂等、失败无害）。

    Args:
        background: ``True`` 时在后台守护线程里预热（不阻塞服务启动）；
            ``False`` 时同步预热（测试 / 离线索引脚本用）。
    """
    global _PREWARMED
    if not jieba_available():
        return
    if _PREWARMED:
        return
    with _PREWARM_LOCK:
        if _PREWARMED:
            return
        _PREWARMED = True

    def _run() -> None:
        try:
            _load_jieba().initialize()
            logger.debug("jieba 词典预热完成")
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响分词兜底
            logger.warning("jieba 预热失败（不影响检索，首次分词会稍慢）：%s", exc)

    if background:
        threading.Thread(target=_run, name="jieba-prewarm", daemon=True).start()
    else:
        _run()


def tokenize(text: str) -> list[str]:
    """中英混合分词，返回小写 token 列表（已去停用词）。

    中文按词（jieba）或字符二元组切分；英文 / 数字 / 编号串整串保留。

    Examples:
        >>> tokenize("年假有几天？")   # doctest: +SKIP
        ['年假', '几天']
        >>> tokenize("OA-2024-017 表单")   # doctest: +SKIP
        ['oa-2024-017', '表单']
    """
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = []

    # 1) 英文 / 数字 / 编号串（整串保留，保证 "oa-2024-017" 不被拆碎）
    for match in _ASCII_TOKEN_RE.finditer(lowered):
        token = match.group(0)
        if token not in _ASCII_STOPWORDS and len(token) <= 64:
            tokens.append(token)

    # 2) 中文片段
    for match in _CJK_RE.finditer(lowered):
        segment = match.group(0)
        if jieba_available():
            tokens.extend(_cut_with_jieba(segment))
        else:
            tokens.extend(_cut_bigrams(segment))

    # 3) 去停用词与超长串
    return [t for t in tokens if t and t not in STOPWORDS and len(t) <= 30]


@lru_cache(maxsize=4096)
def token_set(text: str) -> frozenset[str]:
    """片段的词集合（带缓存）。

    重排、闸门、子问题覆盖都要对同一批候选片段分词，热门片段会被反复切；
    jieba 是纯 Python，长文本每次几毫秒，并发时被 GIL 放大。缓存 4096 段（几十 MB）足够。
    """
    return frozenset(tokenize(text))


def tokenize_for_index(text: str) -> list[str]:
    """索引期分词（与查询期必须使用同一函数，否则倒排对不上）。"""
    return tokenize(text)


def add_words(words: Iterable[str]) -> int:
    """往 jieba 词典里登记行业术语，返回生效条数（jieba 不可用时返回 0）。

    为什么需要它：jieba 默认词典没有各行业的专有词——「年假」会被切成「年」「假」、
    「补卡要」会被错误合并。单字 token 的区分度低，会让 BM25 的 IDF 趋近于 0。
    接入方在 :func:`prewarm` **之前**调用一次即可显著改善（例如
    ``add_words(["年假", "补卡", "报销"])``）。

    注意：:func:`token_set` 有进程内缓存，所以必须在首次分词之前调用；
    这里会顺手清空该缓存，保证返回结果立即生效。
    """
    if not jieba_available() or not words:
        return 0
    try:
        jieba = _load_jieba()
    except Exception as exc:  # noqa: BLE001 - jieba 初始化失败时静默降级（检索仍可用二元组）
        logger.warning("jieba 初始化失败，跳过加词：%s", exc)
        return 0
    added = 0
    for word in words:
        term = str(word).strip()
        if not term:
            continue
        try:
            jieba.add_word(term)
            added += 1
        except Exception as exc:  # noqa: BLE001 - 加词失败不该影响检索
            logger.warning("jieba 加词失败（%r）：%s", term, exc)
    token_set.cache_clear()
    return added


def _cut_with_jieba(segment: str) -> list[str]:
    try:
        jieba = _load_jieba()
        return [piece for piece in jieba.lcut(segment) if piece.strip()]
    except Exception as exc:  # noqa: BLE001 - jieba 异常不应导致检索不可用
        logger.warning("jieba 分词异常，本次回退二元组：%s", exc)
        return _cut_bigrams(segment)


def _cut_bigrams(segment: str) -> list[str]:
    """字符二元组切分。

    * 长度 1：单字自身（让「假」「卡」这类单字查询也能命中）；
    * 长度 2：整词；
    * 长度 ≥3：所有相邻二元组。
    """
    if len(segment) <= 2:
        return [segment]
    return [segment[i:i + 2] for i in range(len(segment) - 1)]


__all__ = [
    "STOPWORDS", "add_words", "jieba_available", "prewarm", "token_set", "tokenize",
    "tokenize_for_index", "tokenizer_name",
]