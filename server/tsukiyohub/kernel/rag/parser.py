"""文档解析：把各种格式的原始文件变成纯文本 :class:`Document`。

设计要点
--------
1. **零依赖格式必须能解析**：``.md`` / ``.txt`` / ``.json`` / ``.csv`` / ``.html`` 只用标准库；
   PDF / DOCX / XLSX / PPTX 走可选依赖，**没装时抛中文 ``ParseError``**，而不是让 ImportError
   冒到上层把整个入库流程打崩（一条坏文件不该影响其它文件）。
2. **编码探测**：中文文档现实里 UTF-8 / GBK / GB18030 混杂，按固定顺序试探比引入 chardet 更可预测；
   全部失败时按 ``utf-8(errors="replace")`` 读——宁可个别乱码，也不要整篇文档进不了库。
3. **元数据可从路径推断**：``doc_type`` 优先用调用方给的；没给时按文件名关键词猜
   （faq / 手册 / 规范 / 通知…），猜不出来落到 ``DocType.DOC``。
4. **稳定 doc_id**：``sha1(相对路径)[:16]``，换机器 / 改标题都不变，重复入库才能幂等覆盖。

Table 保护见 :mod:`tsukiyohub.kernel.rag.chunker`：这里只负责把表格转成 Markdown 表。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.domain import DocType, Document, DocumentMeta, SourceType
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: 可解析的扩展名（API 上传校验与目录扫描共用）
SUPPORTED_SUFFIXES: set[str] = {
    ".md", ".markdown", ".txt", ".text", ".json", ".csv",
    ".html", ".htm", ".pdf", ".docx", ".xlsx", ".xlsm", ".pptx",
}

#: 文本类文件按此顺序尝试解码（UTF-8 → 带 BOM → GBK → GB18030；GB18030 是 GBK 超集，兜老文档）
TEXT_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "gbk", "gb18030")
#: Excel 单表最多收录行数（再多的行对问答没有意义，只会拖慢切块）
XLSX_MAX_ROWS = 2000

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HTML_DROP_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_HTML_BREAK_RE = re.compile(r"<(?:br|/p|/div|/li|/tr|/h[1-6])[^>]*>", re.IGNORECASE)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)

#: 文件名关键词 → 文档类型（通用分类，可按接入方习惯覆盖）
_DOC_TYPE_HINTS: tuple[tuple[str, DocType], ...] = (
    ("faq", DocType.FAQ),
    ("常见问题", DocType.FAQ),
    ("问答", DocType.FAQ),
    ("手册", DocType.SPEC),
    ("规范", DocType.SPEC),
    ("spec", DocType.SPEC),
    ("指南", DocType.SPEC),
    ("通知", DocType.NOTE),
    ("公告", DocType.NOTE),
    ("note", DocType.NOTE),
)


class ParseError(RuntimeError):
    """文件无法解析。``code`` 供上层区分处理（``missing_dependency`` / ``unsupported_type`` …）。"""

    def __init__(self, message: str, code: str = "parse_failed") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 编码与元数据
# ---------------------------------------------------------------------------
def detect_encoding(path: Path, sample_size: int = 65536) -> str:
    """探测文本文件编码，返回**可用的**编码名（全部失败时回退 utf-8，由调用方容错解码）。"""
    data = path.read_bytes()[:sample_size]
    return detect_encoding_bytes(data)


def detect_encoding_bytes(data: bytes) -> str:
    """按 :data:`TEXT_ENCODINGS` 顺序试探字节串编码。"""
    for encoding in TEXT_ENCODINGS:
        try:
            data.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    logger.warning("无法确定文件编码，按 utf-8 容错读取")
    return "utf-8"


def _decode(data: bytes) -> tuple[str, str]:
    """字节 → (文本, 实际编码)；解码不了的字节用 ``replace`` 容错。"""
    encoding = detect_encoding_bytes(data)
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError:
        return data.decode(encoding, errors="replace"), encoding


def guess_doc_type(name: str) -> DocType:
    """按文件名关键词猜文档类型（猜不出来返回 ``DocType.DOC``）。"""
    lowered = (name or "").lower()
    for keyword, doc_type in _DOC_TYPE_HINTS:
        if keyword in lowered:
            return doc_type
    return DocType.DOC


def build_document_meta(
    path: Path,
    *,
    root: Path | None = None,
    source_type: SourceType = SourceType.LOCAL,
    doc_type: DocType = DocType.DOC,
) -> DocumentMeta:
    """从文件路径构造稳定的文档元数据。

    Args:
        path: 文件路径。
        root: 知识库根目录：``doc_id`` 与 ``uri`` 都相对它计算（换机器、改标题都不变）。
        source_type: 来源渠道（本地目录 / 上传 / API 推送）。
        doc_type: 文档类型；传 ``DocType.DOC`` 且文件名有线索时会自动细化。

    Returns:
        :class:`DocumentMeta`；``extra`` 里带 ``file_size`` 便于排障。
    """
    root_path = Path(root).resolve() if root is not None else None
    try:
        resolved = Path(path).resolve()
        relative = resolved.relative_to(root_path) if root_path is not None else Path(resolved.name)
    except (ValueError, OSError):
        resolved = Path(path)
        relative = Path(resolved.name)
    relative_uri = str(relative).replace("\\", "/")
    try:
        mtime = datetime.fromtimestamp(resolved.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
        size = resolved.stat().st_size
    except OSError:
        mtime, size = datetime.now(UTC).isoformat(timespec="seconds"), 0

    resolved_type = doc_type if doc_type is not DocType.DOC else guess_doc_type(resolved.stem)
    return DocumentMeta(
        doc_id=hashlib.sha1(relative_uri.encode("utf-8")).hexdigest()[:16],
        title=resolved.stem,
        source_type=source_type,
        doc_type=resolved_type,
        uri=relative_uri,
        file_name=resolved.name,
        updated_at=mtime,
        extra={"file_size": size},
    )


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    """极简 front matter 解析（``key: value`` 与 ``[a, b]``，不引入 YAML 依赖）。"""
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip().lower()
        value = raw.strip().strip('"').strip("'")
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [item.strip().strip('"').strip("'") for item in value[1:-1].split(",") if item.strip()]
        else:
            meta[key] = value
    return meta, text[match.end():]


def _first_heading(text: str) -> str:
    """Markdown 第一个一级标题（作为标题，比文件名更可读）。"""
    match = _MD_HEADING_RE.search(text)
    return match.group(1).strip()[:200] if match else ""


# ---------------------------------------------------------------------------
# 各格式的加载函数：统一返回 (正文, 标题, loader 标识, 警告)
# ---------------------------------------------------------------------------
LoadResult = tuple[str, str, str, list[str]]


def _load_text(data: bytes) -> LoadResult:
    raw, encoding = _decode(data)
    meta, body = _front_matter(raw)
    title = str(meta.get("title") or "") or _first_heading(body)
    warnings: list[str] = []
    if encoding not in ("utf-8", "utf-8-sig"):
        warnings.append(f"按 {encoding} 解码（文件不是 UTF-8）")
    return body, title, f"text({encoding})", warnings


def _load_json(data: bytes) -> LoadResult:
    """JSON：能解析就缩进成可读文本（切块时按行边界更好断句），解析失败按普通文本处理。"""
    raw, encoding = _decode(data)
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2), "", f"json({encoding})", []
    except ValueError:
        return raw, "", f"text({encoding})", ["JSON 解析失败，已按纯文本入库"]


def _load_html(data: bytes) -> LoadResult:
    """HTML：剥掉脚本 / 样式 / 标签（零依赖，够文档类页面用）。"""
    raw, encoding = _decode(data)
    title_match = _HTML_TITLE_RE.search(raw)
    title = unescape(title_match.group(1)).strip() if title_match else ""
    text = _HTML_DROP_RE.sub("", raw)
    text = _HTML_BREAK_RE.sub("\n", text)
    text = unescape(_HTML_TAG_RE.sub("", text))
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line), title, f"html({encoding})", []


def md_table(rows: list[list[str]]) -> str:
    """二维表 → Markdown 表（首行作表头；空行、全空列裁剪；单元格里的 ``|`` 转义）。"""
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    keep = [index for index in range(width) if any(index < len(row) and row[index].strip() for row in rows)]

    def line(row: list[str]) -> str:
        cells = [(row[index] if index < len(row) else "").replace("|", "\\|") for index in keep]
        return "| " + " | ".join(cells) + " |"

    out = [line(rows[0]), "| " + " | ".join("---" for _ in keep) + " |", *(line(row) for row in rows[1:])]
    return "\n".join(out)


def _load_pdf(data: bytes) -> LoadResult:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ParseError("缺少 PDF 解析依赖：未安装 pypdf，请执行 pip install pypdf", "missing_dependency") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        code = "encrypted" if "encrypt" in str(exc).lower() or "password" in str(exc).lower() else "corrupted"
        message = "PDF 已加密，无法解析" if code == "encrypted" else f"PDF 解析失败：{exc}"
        raise ParseError(message, code) from exc
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    body = "\n\n".join(page for page in pages if page)
    warnings = []
    if sum(1 for page in pages if len(page) < 50) * 2 >= max(1, len(pages)):
        warnings.append("部分页面没有文本层（可能是扫描件），当前内核对扫描件不做 OCR")
    if not body:
        raise ParseError("PDF 没有可提取的文本层（可能是扫描件）", "scanned_pdf")
    return body, "", f"pypdf({len(pages)}页)", warnings


def _load_docx(data: bytes) -> LoadResult:
    try:
        import docx
        from docx.table import Table
    except ImportError as exc:
        raise ParseError(
            "缺少 Word 解析依赖：未安装 python-docx，请执行 pip install python-docx", "missing_dependency"
        ) from exc
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"Word 解析失败：{exc}", "corrupted") from exc

    blocks: list[str] = []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            # 表格往往是制度细节的载体（标准、档位），转成 Markdown 表交给切块器保护
            table = md_table([[(cell.text or "").strip() for cell in row.cells] for row in item.rows])
            if table:
                blocks.append(table)
            continue
        text = (item.text or "").strip()
        if not text:
            continue
        style = (getattr(getattr(item, "style", None), "name", "") or "").lower()
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            blocks.append(f"{'#' * min(int(level), 6)} {text}")
        else:
            blocks.append(text)
    body = "\n\n".join(blocks)
    if not body.strip():
        raise ParseError("Word 文档没有可提取的文本", "empty")
    return body, "", "python-docx", []


def _load_xlsx(data: bytes) -> LoadResult:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ParseError(
            "缺少 Excel 解析依赖：未安装 openpyxl，请执行 pip install openpyxl", "missing_dependency"
        ) from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"Excel 解析失败：{exc}", "corrupted") from exc

    blocks: list[str] = []
    for sheet in workbook.worksheets:
        rows = [[("" if cell is None else str(cell)).strip() for cell in row] for row in sheet.iter_rows(values_only=True)]
        rows = [row for row in rows if any(row)]
        if not rows:
            continue
        blocks.append(f"## 工作表：{sheet.title}")
        blocks.append(md_table(rows[: XLSX_MAX_ROWS + 1]))
        if len(rows) > XLSX_MAX_ROWS + 1:
            blocks.append(f"（超过 {XLSX_MAX_ROWS} 行，其余行未收录）")
    body = "\n\n".join(block for block in blocks if block)
    if not body.strip():
        raise ParseError("Excel 没有可提取的单元格内容", "empty")
    return body, "", "openpyxl", []


def _load_csv(data: bytes) -> LoadResult:
    raw, encoding = _decode(data)
    try:
        rows = [row for row in csv.reader(io.StringIO(raw)) if any(cell.strip() for cell in row)]
    except csv.Error as exc:
        raise ParseError(f"CSV 解析失败：{exc}", "corrupted") from exc
    body = md_table(rows[: XLSX_MAX_ROWS + 1])
    if not body.strip():
        raise ParseError("CSV 没有可提取的内容", "empty")
    return body, "", f"csv({encoding})", []


def _load_pptx(data: bytes) -> LoadResult:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise ParseError(
            "缺少 PPT 解析依赖：未安装 python-pptx，请执行 pip install python-pptx", "missing_dependency"
        ) from exc
    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"PPT 解析失败：{exc}", "corrupted") from exc

    blocks: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        texts = [shape.text.strip() for shape in slide.shapes if getattr(shape, "has_text_frame", False)]
        texts = [text for text in texts if text]
        if texts:
            blocks.append(f"## 第 {index} 页\n" + "\n".join(texts))
    body = "\n\n".join(blocks)
    if not body.strip():
        raise ParseError("PPT 没有可提取的文本", "empty")
    return body, "", "python-pptx", []


_LOADERS: dict[str, Any] = {
    ".md": _load_text,
    ".markdown": _load_text,
    ".txt": _load_text,
    ".text": _load_text,
    ".json": _load_json,
    ".html": _load_html,
    ".htm": _load_html,
    ".csv": _load_csv,
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".xlsx": _load_xlsx,
    ".xlsm": _load_xlsx,
    ".pptx": _load_pptx,
}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
class DocumentParser:
    """按扩展名把文件解析成 :class:`Document`（纯文本 + 元数据 + 内容哈希）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ 公开
    def parse(self, path: Path, meta: DocumentMeta) -> Document:
        """解析本地文件。扩展名不支持 / 依赖缺失 / 文件损坏都会抛中文 :class:`ParseError`。"""
        data = Path(path).read_bytes()
        return self.parse_bytes(meta.file_name or Path(path).name, data, meta)

    def parse_bytes(self, file_name: str, data: bytes, meta: DocumentMeta | None = None) -> Document:
        """解析内存中的文件内容（上传、对象存储、测试都走这里）。

        Args:
            file_name: 文件名（决定用哪个加载器与标题兜底）。
            data: 原始字节。
            meta: 已知元数据；``None`` 时用文件名合成一份（doc_id 取文件名哈希）。
        """
        suffix = Path(file_name).suffix.lower()
        loader = _LOADERS.get(suffix)
        if loader is None:
            raise ParseError(
                f"不支持的格式 {suffix or '(无扩展名)'}；当前支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}",
                "unsupported_type",
            )
        resolved_meta = meta or self._meta_from_name(file_name)
        try:
            text, title, loader_name, warnings = loader(data)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - 未预期异常也要变成可读中文错误
            raise ParseError(f"{file_name} 解析失败：{exc}", "parse_failed") from exc

        content = (text or "").strip()
        if not content:
            raise ParseError(f"{file_name} 解析后没有有效文本", "empty")

        resolved_meta.title = (title or resolved_meta.title or Path(file_name).stem)[:200]
        extra: dict[str, Any] = {**resolved_meta.extra, "loader": loader_name}
        if warnings:
            extra["warnings"] = warnings
        resolved_meta.extra = extra
        if resolved_meta.doc_type is DocType.DOC:
            resolved_meta.doc_type = guess_doc_type(Path(file_name).stem)
        return Document(
            meta=resolved_meta,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def can_parse(suffix: str) -> bool:
        """该扩展名是否在支持列表里（不检测可选依赖是否安装）。"""
        return suffix.lower() in SUPPORTED_SUFFIXES

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def meta_from_name(file_name: str) -> DocumentMeta:
        """仅凭文件名合成元数据（上传场景：``doc_id = sha1("upload|文件名")[:16]``）。

        用文件名而不是内容做 ID，是为了「同一份文件重复上传 = 覆盖更新」而不是堆副本。
        """
        return DocumentParser._meta_from_name(file_name)

    @staticmethod
    def _meta_from_name(file_name: str) -> DocumentMeta:
        name = Path(file_name).name
        return DocumentMeta(
            doc_id=hashlib.sha1(f"upload|{name}".encode()).hexdigest()[:16],
            title=Path(name).stem,
            source_type=SourceType.UPLOAD,
            doc_type=guess_doc_type(Path(name).stem),
            uri=name,
            file_name=name,
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def parse_file(path: Path, *, root: Path | None = None, source_type: SourceType = SourceType.LOCAL,
               doc_type: DocType = DocType.DOC, settings: Settings | None = None) -> Document:
    """便捷函数：路径 → 文档（元数据自动推断）。批量导入脚本 / 测试用。"""
    meta = build_document_meta(path, root=root, source_type=source_type, doc_type=doc_type)
    parser = DocumentParser(settings or _default_settings())
    return parser.parse(path, meta)


def _default_settings() -> Settings:
    from tsukiyohub.config import get_settings

    return get_settings()


def parse_bytes(file_name: str, data: bytes, *, settings: Settings | None = None,
                source_type: SourceType = SourceType.UPLOAD) -> Document:
    """便捷函数：字节 → 文档。"""
    meta = DocumentParser._meta_from_name(file_name)
    meta.source_type = source_type
    parser = DocumentParser(settings or _default_settings())
    return parser.parse_bytes(file_name, data, meta)


__all__ = [
    "SUPPORTED_SUFFIXES", "TEXT_ENCODINGS", "DocumentParser", "ParseError",
    "build_document_meta", "detect_encoding", "detect_encoding_bytes", "guess_doc_type",
    "md_table", "parse_bytes", "parse_file",
]