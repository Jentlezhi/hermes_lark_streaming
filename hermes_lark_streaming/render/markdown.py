"""Markdown 处理 — 切分、表格降级、图片引用识别.

三个职责：

1. **长文本切分**：飞书单个 markdown 元素有长度上限，超长要按结构边界切，
   不能在代码围栏或表格中间截断，否则渲染成半截原文
2. **表格降级**：飞书对宽表格渲染不佳，超列表格转成字段列表，信息无损
3. **图片引用识别**：找出 markdown 图片语法，交由传输层上传换取 img_key
"""

from __future__ import annotations

import re
from typing import Final

#: 单个 markdown 元素的安全长度上限（飞书未公开硬值，取保守值）
MAX_ELEMENT_CHARS: Final[int] = 4000
#: 表格超过该列数时降级为字段列表
TABLE_DOWNGRADE_COLUMNS: Final[int] = 5

_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(`{3,}|~{3,})")
_TABLE_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_IMAGE_RE: Final[re.Pattern[str]] = re.compile(r"!\[([^\]]*)\]\(\s*(<?)([^)\s]+)\2\s*\)")


def split_long_text(text: str, limit: int = MAX_ELEMENT_CHARS) -> list[str]:
    """按结构边界把长文本切成多块.

    切分优先级：代码围栏外的空行 > 换行 > 硬切。围栏内绝不切分，
    否则会产生未闭合的代码块，飞书会把后续内容全渲染成代码。
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    fence: str | None = None

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        match = _FENCE_RE.match(stripped)
        if match:
            marker = match.group(1)[0] * 3
            if fence is None:
                fence = marker
            elif stripped.startswith(fence):
                fence = None

        line_len = len(line)
        # 只在围栏外、且已有内容时才允许切块
        if fence is None and current_len + line_len > limit and current:
            chunks.append("".join(current))
            current = []
            current_len = 0

        # 单行本身就超限（无换行的超长行）：硬切
        if line_len > limit:
            if current:
                chunks.append("".join(current))
                current = []
                current_len = 0
            for start in range(0, line_len, limit):
                chunks.append(line[start : start + limit])
            continue

        current.append(line)
        current_len += line_len

    if current:
        chunks.append("".join(current))

    # 围栏跨块时补齐闭合，保证每一块都是合法 markdown
    return _balance_fences(chunks)


def _balance_fences(chunks: list[str]) -> list[str]:
    """为跨块的代码围栏补齐闭合与重开，避免半截围栏."""
    balanced: list[str] = []
    carry: str | None = None

    for chunk in chunks:
        body = chunk
        if carry:
            body = f"{carry}\n{body}"
        fence: str | None = None
        for line in body.splitlines():
            stripped = line.strip()
            match = _FENCE_RE.match(stripped)
            if not match:
                continue
            marker = match.group(1)[0] * 3
            if fence is None:
                fence = marker
            elif stripped.startswith(fence):
                fence = None
        if fence is not None:
            body = f"{body}\n{fence}"
            carry = fence
        else:
            carry = None
        balanced.append(body)
    return balanced


def downgrade_wide_tables(text: str, max_columns: int = TABLE_DOWNGRADE_COLUMNS) -> str:
    """把过宽的 markdown 表格降级为字段列表.

    飞书卡片对宽表格的渲染会横向溢出甚至退化成原始文本。转成
    「**表头**：值」的列表后信息完全保留，且窄屏可读。
    """
    if "|" not in text:
        return text

    lines = text.splitlines()
    output: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        # 表格特征：当前行是表格行，且下一行是分隔行
        if (
            _TABLE_ROW_RE.match(line)
            and index + 1 < total
            and _TABLE_SEP_RE.match(lines[index + 1])
        ):
            header = _split_row(line)
            if len(header) <= max_columns:
                output.append(line)
                index += 1
                continue

            # 收集整张表
            body_index = index + 2
            rows: list[list[str]] = []
            while body_index < total and _TABLE_ROW_RE.match(lines[body_index]):
                rows.append(_split_row(lines[body_index]))
                body_index += 1

            output.extend(_render_as_fields(header, rows))
            index = body_index
            continue

        output.append(line)
        index += 1

    return "\n".join(output)


def _split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def _render_as_fields(header: list[str], rows: list[list[str]]) -> list[str]:
    """表格转字段列表."""
    rendered: list[str] = []
    for position, row in enumerate(rows, start=1):
        rendered.append(f"**{position}.**")
        for column_index, name in enumerate(header):
            value = row[column_index] if column_index < len(row) else ""
            if value:
                rendered.append(f"- **{name}**：{value}")
        rendered.append("")
    return rendered


def normalize_markdown(text: str) -> str:
    """统一换行并去除首尾空白，保证元素内容稳定."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def find_image_refs(text: str) -> list[str]:
    """提取 markdown 图片引用的 URL 列表（去重且保序）."""
    if "![" not in text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for match in _IMAGE_RE.finditer(text):
        url = match.group(3).strip()
        if url and url not in seen and not url.startswith("img_"):
            seen.add(url)
            urls.append(url)
    return urls


def replace_image_refs(text: str, mapping: dict[str, str]) -> str:
    """把图片 URL 替换为飞书 img_key.

    未成功上传的图片保持原样（仍显示为链接），不因单张图失败而丢内容。
    """
    if not mapping or "![" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        url = match.group(3).strip()
        img_key = mapping.get(url)
        if not img_key:
            return match.group(0)
        alt = match.group(1)
        return f"![{alt}]({img_key})"

    return _IMAGE_RE.sub(_replace, text)


def escape_inline(value: str) -> str:
    """转义会破坏 markdown 结构的字符，用于把用户内容嵌入标题等位置."""
    return re.sub(r"([`*_{}\[\]<>])", r"\\\1", value.replace("\\", "\\\\"))


def code_block(content: str, language: str = "text") -> str:
    """构造代码块，围栏长度自适应内容中的反引号."""
    normalized = normalize_markdown(content)
    longest = max((len(match) for match in re.findall(r"`+", normalized)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{normalized}\n{fence}"
