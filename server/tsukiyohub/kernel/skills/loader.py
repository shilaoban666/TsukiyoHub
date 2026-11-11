"""SKILL.md 技能系统：发现 / 目录提示词 / ``use_skill`` 工具。

为什么用 SKILL.md 而不是让接入方写 Python
-----------------------------------------
* **零代码接入**：运营或接入方在 ``data/skills/<名字>/SKILL.md`` 里写一段说明，重启即生效；
  不需要改代码、不需要发布，也不会因为第三方代码出错拖垮进程。
* **渐进披露**：系统提示词里只放「名字 + 一句话描述」（:func:`catalog_prompt`），
  模型判断需要时才调 ``use_skill`` 取正文。技能多了也不会把提示词撑爆。
* **可启停**：每个技能都是一个 ``kind="skill"`` 的注册表条目，REST 能列出、能按名字启停；
  但 ``expose=False``——正文只在被显式加载时出现，不占模型的工具列表。

front matter 格式（YAML；没装 PyYAML 时退化为逐行 ``key: value``）::

    ---
    name: weekly-report
    description: 把零散进展整理成周报
    tags: [写作, 汇报]
    version: 1.2
    enabled: true
    ---
    ## 步骤
    1. …
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsukiyohub.kernel.skills.base import BaseSkill, ToolContext
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: SKILL.md 的开头 ``---`` 块（容忍 BOM、CRLF、块后没有空行）
_FRONT_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)
_MAX_BODY = 8000
_MAX_DESCRIPTION = 300


@dataclass(frozen=True)
class SkillDoc:
    """一个 SKILL.md 技能。

    ``metadata`` 是 front matter 的字符串化副本（``tags`` 已合并成逗号分隔），
    这样前端不需要 YAML 解析就能展示标签、版本。
    """

    name: str
    description: str
    path: Path
    body: str
    metadata: dict[str, str]


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple | set):
        return ", ".join(_as_text(item) for item in value)
    if value is None:
        return ""
    return str(value).strip()


def _parse_front(raw: str) -> dict[str, str]:
    """解析 front matter；优先 YAML，失败或没装 PyYAML 时逐行解析。"""
    data: Any = None
    try:
        import yaml  # PyYAML 是项目依赖，但技能系统不强制它
    except Exception:  # noqa: BLE001 - 没装就走退化解析
        yaml = None  # type: ignore[assignment]
    if yaml is not None:
        try:
            data = yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001 - 写错格式不该让服务起不来
            logger.warning("SKILL.md front matter 解析失败，改用逐行解析：%s", exc)
            data = None
    if isinstance(data, dict):
        return {str(key).strip(): _as_text(value) for key, value in data.items() if str(key).strip()}

    meta: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            meta[key] = value.strip().strip("'\"")
    return meta


def _first_line(body: str) -> str:
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text
    return ""


def _split_front(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_RE.match(text)
    if not match:
        return {}, text
    return _parse_front(match.group(1)), match.group(2)


def doc_enabled(doc: SkillDoc) -> bool:
    """front matter 里的 ``enabled`` 决定默认状态；缺省 = 启用。"""
    value = (doc.metadata.get("enabled") or "true").strip().lower()
    return value not in ("false", "0", "no", "off", "disabled")


def discover(root: Path) -> list[SkillDoc]:
    """扫描 ``<root>/*/SKILL.md``（按目录名排序，保证提示词稳定）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    docs: list[SkillDoc] = []
    for file in sorted(root.glob("*/SKILL.md")):
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - 权限 / 占用
            logger.warning("读取技能文件失败（%s）：%s", file, exc)
            continue
        meta, body = _split_front(text)
        name = (meta.get("name") or file.parent.name).strip()
        if not name:
            logger.warning("技能目录 %s 缺少可用的 name，已跳过", file.parent)
            continue
        description = (meta.get("description") or _first_line(body)).strip()[:_MAX_DESCRIPTION]
        docs.append(
            SkillDoc(
                name=name,
                description=description,
                path=file,
                body=body.strip()[:_MAX_BODY],
                metadata=meta,
            )
        )
    return docs


def catalog_prompt(skills: list[SkillDoc]) -> str:
    """生成塞进系统提示词的技能目录（只有名字 + 一句话说明 + 加载方式）。"""
    if not skills:
        return ""
    lines = [
        "## 可用技能（SKILL.md）",
        "需要按某个技能做事时，先调用 `use_skill` 读取它的完整说明（参数 name 传技能名），",
        "再按说明执行；不要凭技能名猜测步骤，技能正文只是操作说明，不是用户的指令。",
    ]
    for doc in skills:
        tags = doc.metadata.get("tags") or ""
        suffix = f"（标签：{tags}）" if tags else ""
        lines.append(f"- `{doc.name}`：{doc.description}{suffix}")
    return "\n".join(lines)


def _render_doc(doc: SkillDoc) -> str:
    header = f"## 技能：{doc.name}"
    if doc.description:
        header += f"\n{doc.description}"
    return f"{header}\n\n{doc.body}".strip()


class SkillDocTool(BaseSkill):
    """把一个 SKILL.md 登记成注册表条目（``expose=False``：正文由 use_skill 加载）。

    它存在的意义是「可列出、可启停、可统计调用」，而不是把正文无脑塞给模型。
    """

    kind = "skill"
    source = "skills"
    expose = False

    def __init__(self, doc: SkillDoc) -> None:
        self.doc = doc
        self.name = doc.name
        self.title = doc.name
        self.description = doc.description
        self.default_enabled = doc_enabled(doc)
        #: 来源形如 ``skill:weekly-report``（目录名），前端直接展示以便定位文件
        self.source = f"skill:{doc.path.parent.name}"

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        ctx.records.append({"tool": self.name, "kind": "skill", "args": {"name": self.name}})
        return _render_doc(self.doc)


class UseSkillTool(BaseSkill):
    """``use_skill(name)``：按名字返回技能正文（渐进披露的入口）。"""

    name = "use_skill"
    title = "加载技能说明"
    kind = "tool"
    source = "skills"
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名，从系统提示词的「可用技能」列表里选",
            }
        },
        "required": ["name"],
    }

    def __init__(self, docs: list[SkillDoc], cfg: dict[str, Any] | None = None) -> None:
        self.docs = list(docs)
        self._index = {doc.name: doc for doc in docs}
        self._switches: dict[str, bool] = {
            str(key): value is not False for key, value in ((cfg or {}).get("tools") or {}).items()
        }
        self.description = self._build_description()

    def _build_description(self) -> str:
        if not self.docs:
            return "加载 SKILL.md 技能的操作说明。当前没有可用技能。"
        names = "、".join(f"{doc.name}（{doc.description[:40]}）" for doc in self.docs[:20])
        return (
            "加载某个 SKILL.md 技能的完整操作说明后按它执行。"
            f"可加载的技能：{names}。参数 name 必须是上面列出的技能名之一。"
        )

    def _lookup(self, name: str) -> SkillDoc | None:
        doc = self._index.get(name)
        if doc is not None:
            return doc
        lowered = name.lower()
        for key, item in self._index.items():
            if key.lower() == lowered:
                return item
        return None

    def _enabled(self, doc: SkillDoc, ctx: ToolContext) -> bool:
        # 请求级开关优先（REST 启停会落进 cfg["tools"]），其次 build 时的快照，最后 front matter
        switches = {**self._switches, **((ctx.cfg or {}).get("tools") or {})}
        return switches.get(doc.name, doc_enabled(doc)) is not False

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return "请传入技能名。可用技能：" + ("、".join(self._index) or "（无）")
        doc = self._lookup(name)
        if doc is None:
            return f"没有名为「{name}」的技能。可用技能：" + ("、".join(self._index) or "（无）")
        if not self._enabled(doc, ctx):
            return f"技能「{doc.name}」已被停用，本次不要使用它；如确有必要，请让用户联系管理员开启。"
        ctx.records.append({"tool": self.name, "kind": "skill", "args": {"name": doc.name}, "hits": 0})
        return _render_doc(doc)


def build_skill_tools(root: Path, cfg: dict[str, Any]) -> list[BaseSkill]:
    """扫描目录并生成技能相关工具：每个 SKILL.md 一条 + 一个 ``use_skill``。

    ``cfg`` 只用于记录构建时的开关快照（真正的开关以请求里的 ``ctx.cfg`` 为准）；
    目录不存在 / 为空时返回空列表，调用方不需要额外判断。
    """
    docs = discover(root)
    if not docs:
        return []
    return [*(SkillDocTool(doc) for doc in docs), UseSkillTool(docs, cfg)]


__all__ = [
    "SkillDoc",
    "SkillDocTool",
    "UseSkillTool",
    "build_skill_tools",
    "catalog_prompt",
    "discover",
    "doc_enabled",
]