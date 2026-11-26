"""Terminal-cell layout for the session list. Apply SGR only after fitting text."""

import os
import unicodedata
from datetime import datetime

from .catalog import clean, display_title, project_root

LABEL = {"codex": "Codex", "claude": "Claude Code", "agy": "agy"}
SOURCE_COLOR = {"codex": "36", "claude": "33", "agy": "35"}


def cell_width(ch):
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width_of(text):
    return sum(map(cell_width, text))


def clip(text, width):
    if width <= 0:
        return ""
    used = 0
    for i, ch in enumerate(text):
        used += cell_width(ch)
        if used > width:
            return text[:i]
    return text


def shorten(text, width, *, tail=False):
    if width <= 0:
        return ""
    if width_of(text) <= width:
        return text
    mark = "." * min(3, width)
    if tail:
        suffix = clip(text[::-1], width - len(mark))[::-1]
        while suffix and cell_width(suffix[0]) == 0:
            suffix = suffix[1:]
        return mark + suffix
    return clip(text, width - len(mark)) + mark


def field(text, width):
    value = shorten(text, width)
    return value + " " * (width - width_of(value))


def time_label(row, pattern):
    return datetime.fromtimestamp(row["updated"]).strftime(pattern) if row["updated"] else "—"


def page_ranges(groups, capacity):
    """Keep each group heading with its first row, including across page breaks."""
    start = used = 0
    for i, group in enumerate(groups):
        heading = group is not None and (i == start or group != groups[i - 1])
        cost = 1 + int(heading)
        if used + cost > capacity:
            yield start, i
            start, used = i, 0
            cost = 1 + int(group is not None)
        used += cost
    yield start, len(groups)


def render_picker(rows, index, query, pending, errors, size, project, by_project):
    height, width = (max(1, n) for n in size)
    margin = 2 if width >= 16 else 0
    inner = width - 2 * margin
    inset = " " * margin
    use_style = not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"

    def paint(text, sgr):
        return f"\x1b[{sgr}m{text}\x1b[0m" if use_style and text else text

    def paired(left, right, style, *, tail=False):
        space = inner - width_of(right) - 2
        if not right or space < 5:
            return inset + paint(shorten(left, inner, tail=tail), style)
        value = shorten(left, space, tail=tail)
        gap = " " * (inner - width_of(value) - width_of(right))
        return inset + paint(value, style) + gap + paint(right, "2")

    # A row of East Asian Ambiguous box-drawing glyphs can wrap twice.
    rule = inset + (paint(" " * inner, "2;4") if use_style else "-" * inner)
    count = f"{len(rows)} 个会话"
    mode = "项目视图" if by_project else "时间视图"
    title = "引用会话  /  " + mode if inner >= 32 else mode
    header = [paired(title, count, "1;36")]
    search_label = clip("搜索  ", inner)
    hint = "输入标题或项目名…" if width >= 48 else "输入关键词"
    search = shorten(query or hint, inner - width_of(search_label), tail=bool(query))
    header.append(inset + paint(search_label, "1") + paint(search, "0" if query else "2"))

    # Reserve the title column first; introduce secondary columns as space allows.
    source_width = 11 if width >= 56 else min(6, max(0, inner - 6))
    time_width = 11 if width >= 54 else 0
    project_width = 18 if width >= 80 and not by_project else 0
    columns = [w for w in (source_width, project_width, time_width) if w]
    title_width = max(0, inner - 2 - sum(w + 2 for w in columns))

    def table_line(row, selected=False):
        heading = row is None
        if inner < 8:
            return inset + clip("›" + display_title(row) if selected else
                                ("标题" if heading else display_title(row)), inner)
        source = "来源" if heading else LABEL[row["source"]]
        if not heading and source_width < 11 and row["source"] == "claude":
            source = "Claude"
        title = "会话标题" if heading else display_title(row)
        cells = []
        if source_width:
            source = field(source, source_width)
            if not heading and not selected:
                source = paint(source, SOURCE_COLOR[row["source"]])
            cells.append(source)
        cells.append(field(title, title_width))
        if project_width:
            name = "项目" if heading else clean(os.path.basename(project_root(row["project"]))) or "—"
            cells.append(field(name, project_width))
        if time_width:
            cells.append(field("更新 ↓" if heading else time_label(row, "%m-%d %H:%M"), time_width))
        line = ("› " if selected else "  ") + "  ".join(cells)
        if selected:
            return inset + paint(line, "7")
        return inset + (paint(line, "2") if heading else line)

    if height >= 10:
        if height >= 14:
            header.append(rule)
        header.append(table_line(None))
    notices = [f"{LABEL[s]} · {msg}" for s, msg in sorted(errors.items())]
    if pending:
        notices.append("读取中  " + " / ".join(LABEL[s] for s in sorted(pending)))
    notice_count = max(0, height - len(header) - 5)
    if errors and height >= 5:
        notice_count = max(1, notice_count)
    for notice in notices[:notice_count]:
        header.append(inset + paint(shorten(notice, inner), "2"))

    details = []
    if rows:
        selected = rows[index]
        path = selected["project"] or "—"
        home = os.path.expanduser("~")
        if path == home or path.startswith(home + "/"):
            path = "~" + path[len(home):]
        date = time_label(selected, "%Y-%m-%d %H:%M:%S")
        if width >= 54:
            details.append(paired(path, date, "0", tail=True))
        else:
            details.extend(inset + paint(shorten(s, inner, tail=True), "2") for s in (path, date))

    footer = [rule, *details] if details else []
    key_hint = "Tab 切换视图   ↑↓ 选择   Enter 插入   Esc 取消" if width >= 54 else "Tab 切换  ↵ 插入  Esc 退"
    if inner < width_of(key_hint):
        key_hint = "Tab / ↵ / Esc"
    # On very short screens keep the search and at least one result on screen.
    if height < 5:
        header = header[1:2] if height > 1 else []
        if errors and rows and height > 1:
            header = header[:max(0, height - 2)] + [inset + paint(shorten(notices[0], inner), "2")]
    footer_budget = max(0, height - len(header) - 2)
    if len(footer) > footer_budget:
        footer = details[-footer_budget:] if footer_budget else []
    show_keys = height >= 5
    capacity = max(1, height - len(header) - len(footer) - int(show_keys))
    roots = [project_root(row["project"]) for row in rows]
    groups = [None] * len(rows)
    if capacity >= 2:
        if by_project:
            groups = roots
        elif project in roots:
            groups = [project if root == project else "" for root in roots]
    pages = list(page_ranges(groups, capacity))
    page_no = next((n for n, (_, end) in enumerate(pages) if index < end), 0)
    start, end = pages[page_no]
    project_names = [os.path.basename(root) for root in set(roots) if root]
    body = []
    for i in range(start, end):
        group = groups[i]
        if group is not None and (i == start or group != groups[i - 1]):
            if group == project:
                label = "当前项目 · " + (os.path.basename(project) or project)
            elif by_project:
                name = os.path.basename(group)
                label = name if name and project_names.count(name) == 1 else group or "未知项目"
            else:
                label = "其他项目"
            body.append(inset + paint(shorten(clean(label), inner, tail=True), "1;36" if group == project else "1"))
        body.append(table_line(rows[i], i == index))
    if not body:
        message = "正在读取会话…" if pending else "没有匹配的会话"
        if errors and not notice_count:
            message = notices[0]
        elif len(errors) == len(LABEL):
            message = "会话来源均读取失败"
        body.append(inset + paint(shorten(message, inner), "2"))
    if show_keys:
        position = f"{page_no + 1}/{len(pages)}" if len(pages) > 1 and inner >= width_of(key_hint) + 7 else ""
        footer.append(paired(key_hint, position, "2"))
    lines = header + body + [""] * max(0, height - len(header) - len(body) - len(footer)) + footer
    frame = "".join(f"\x1b[{i};1H\x1b[2K{line}" for i, line in enumerate(lines[:height], 1))
    def target(n):
        if n < 0:
            return 0
        if n >= len(pages):
            return max(0, len(rows) - 1)
        first, last = pages[n]
        return min(first + index - start, last - 1)

    return frame.encode(), index - target(page_no - 1), target(page_no + 1) - index
