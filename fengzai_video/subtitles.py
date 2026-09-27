"""字幕：脚本拆句、对时间轴、关键词高亮，生成 ASS 字幕文件（顶部标题 + 口播字幕 + 角标）。"""
from __future__ import annotations

import math
import re

PUNCT = r"[，。！？!?,.、；;：:…～~\s\"“”'‘’（）()《》【】]+"
_ROLE_PREFIX = re.compile(r"^\s*[^\s：:，。！？]{1,6}[：:]")
_TIMED_PREFIX = re.compile(r"\d+(?:\.\d+)?\s*[-~～到]\s*\d+(?:\.\d+)?\s*秒?\s*(?:[（(][^）)]*[）)])?\s*[：:]")


def script_to_lines(script: str) -> list[str]:
    """去掉"女业主：""0-4秒（女业主）："这类前缀，只保留要念的内容。"""
    text = _TIMED_PREFIX.sub("\n", script)
    lines = []
    for line in text.splitlines():
        line = _ROLE_PREFIX.sub("", line).strip()
        if line:
            lines.append(line)
    return lines


# 家装常用词，避免字幕把它们从中间断开
DOMAIN_WORDS = [
    "评论区", "卫生间", "人工材料", "包工包料", "上门施工", "一站式", "无缝一体", "老房翻新", "局部翻新",
    "旧基层", "防潮防滑", "石英石", "油烟机", "瓷砖", "墙砖", "地砖", "晶瓷", "留个1", "留言1",
]  # fmt: skip
BREAK_AFTER = set("的了后啦呢吧呀啊嘛")
BREAK_BEFORE = set("才就也都还又却但所")
NO_BREAK_AFTER = set("别不没很太更再这那")
NO_BREAK_BEFORE = set("们啦吧呢啊呀了的")

try:
    import logging as _logging

    import jieba

    jieba.setLogLevel(_logging.WARNING)
    for _w in DOMAIN_WORDS:
        jieba.add_word(_w)
except ImportError:  # 没装 jieba 时退化为按字切分
    jieba = None


def _word_bounds(piece: str, protect: list[str]) -> tuple[set[int], set[int]]:
    """返回 (词语边界位置, 禁止切断的位置)。"""
    bounds = set(range(1, len(piece)))
    if jieba is not None:
        bounds, pos = set(), 0
        for w in jieba.lcut(piece):
            pos += len(w)
            bounds.add(pos)
    blocked = set()
    for kw in protect + DOMAIN_WORDS:
        start = piece.find(kw)
        while kw and start != -1:
            blocked.update(range(start + 1, start + len(kw)))
            start = piece.find(kw, start + 1)
    return bounds, blocked


def _chunk(piece: str, max_chars: int, protect: list[str]) -> list[str]:
    if len(piece) <= max_chars:
        return [piece]
    n = math.ceil(len(piece) / max_chars)
    target = len(piece) / n
    bounds, blocked = _word_bounds(piece, protect)

    def score(i: int) -> float:
        s = abs(i - target) * 0.2
        s += 0 if i in bounds else 3
        s += 100 if i in blocked else 0
        s -= 0.3 if piece[i - 1] in BREAK_AFTER else 0
        s -= 0.6 if piece[i] in BREAK_BEFORE else 0
        s += 2 if piece[i - 1] in NO_BREAK_AFTER or piece[i] in NO_BREAK_BEFORE else 0
        s += 1.5 if min(i, len(piece) - i) < 3 else 0
        return s

    lo, hi = max(1, len(piece) - (n - 1) * max_chars), min(max_chars, len(piece) - 1)
    cut = min(range(lo, hi + 1), key=score)
    return [piece[:cut]] + _chunk(piece[cut:], max_chars, protect)


def split_phrases(script: str, max_chars: int = 9, protect: list[str] | None = None) -> list[str]:
    """按标点拆句，长句在词语边界处断开，每条字幕不超过 max_chars 个字。"""
    phrases: list[str] = []
    for line in script_to_lines(script):
        for piece in re.split(PUNCT, line):
            if piece:
                phrases.extend(_chunk(piece, max_chars, protect or []))
    return phrases


def allocate_times(
    phrases: list[str], voiced: list[tuple[float, float]], duration: float, bridge_gap: float = 0.6
) -> list[tuple[float, float]]:
    """按字数把每句分配到"有人声"的时间段上；句间短停顿不让字幕闪断。"""
    if not phrases:
        return []
    voiced = [(max(0.0, s), min(duration, e)) for s, e in voiced if e - s > 0.05] or [(0.0, duration)]
    total_voiced = sum(e - s for s, e in voiced)

    def at(frac: float, is_start: bool) -> float:
        # 恰好落在两段人声交界时：句子开头取下一段的起点，句子结尾取上一段的终点
        remaining = frac * total_voiced
        for s, e in voiced:
            if remaining < e - s or (not is_start and remaining <= e - s):
                return s + remaining
            remaining -= e - s
        return voiced[-1][1]

    weights = [max(len(p), 1) for p in phrases]
    total = sum(weights)
    times, acc = [], 0
    for w in weights:
        times.append([at(acc / total, True), at((acc + w) / total, False)])
        acc += w
    for i in range(len(times) - 1):
        if times[i + 1][0] - times[i][1] < bridge_gap:
            times[i][1] = times[i + 1][0]
    times[-1][1] = min(duration, times[-1][1] + 0.3)
    return [(round(s, 2), round(e, 2)) for s, e in times]


def ass_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "（").replace("}", "）").replace("\n", " ")


def highlight(phrase: str, keywords: list[str], color_hex: str) -> str:
    """关键词换成高亮色（最长优先、互不重叠）。"""
    marks = [False] * len(phrase)
    for kw in sorted({k for k in keywords if k}, key=len, reverse=True):
        start = phrase.find(kw)
        while start != -1:
            if not any(marks[start : start + len(kw)]):
                for i in range(start, start + len(kw)):
                    marks[i] = True
            start = phrase.find(kw, start + 1)
    out, on = [], False
    for ch, m in zip(phrase, marks):
        if m != on:
            out.append("{\\c" + (ass_color(color_hex) if m else "&H00FFFFFF") + "&}")
            on = m
        out.append(_clean(ch))
    if on:
        out.append("{\\c&H00FFFFFF&}")
    return "".join(out)


def fit_size(text: str, base: float, max_width: float) -> float:
    """按字宽计算：标题太长时自动缩小，保证一行放得下。返回单个汉字的像素宽。"""
    units = sum(0.55 if ord(c) < 128 else 1.0 for c in text) or 1
    return min(base, max_width / units)


_RATIO_CACHE: dict[tuple[str, str], float] = {}


def em_ratio(family: str, fonts_dir: str = "") -> float:
    """ASS 字号换算成汉字实际宽度的比例（每款字体不同）。读不到字体时按 0.85 估算。"""
    key = (family, fonts_dir)
    if key in _RATIO_CACHE:
        return _RATIO_CACHE[key]
    ratio = 0.85
    try:
        import subprocess
        from pathlib import Path

        from fontTools.ttLib import TTCollection, TTFont

        files = [p for p in Path(fonts_dir).glob("*") if p.suffix.lower() in {".ttf", ".otf", ".ttc"}] if fonts_dir else []
        try:
            matched = subprocess.run(
                ["fc-match", "-f", "%{file}", family], capture_output=True, text=True, encoding="utf-8", errors="replace"
            ).stdout
        except OSError:  # Windows 上没有 fc-match
            matched = ""
        if matched:
            files.append(Path(matched))
        for path in files:
            fonts = TTCollection(str(path)).fonts if path.suffix.lower() == ".ttc" else [TTFont(str(path))]
            for font in fonts:
                names = {str(n) for n in font["name"].names if n.nameID in (1, 4)}
                if family in names:
                    os2 = font["OS/2"]
                    ratio = font["head"].unitsPerEm / (os2.usWinAscent + os2.usWinDescent)
                    raise StopIteration
    except StopIteration:
        pass
    except Exception:
        pass
    _RATIO_CACHE[key] = ratio
    return ratio


def _ts(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(
    width: int,
    height: int,
    duration: float,
    top_title: str,
    subtitle: str,
    captions: list[tuple[float, float, str]],
    render: dict,
    center_text: str = "",
) -> str:
    s = width / 1080
    title_font, caption_font = render["title_font"], render["caption_font"]
    yellow = ass_color(render["title_color"])
    black, white = "&H00000000", "&H00FFFFFF"
    max_w = width * 0.9
    tr = em_ratio(title_font, render.get("fonts_dir", ""))
    cr = em_ratio(caption_font, render.get("fonts_dir", ""))

    # 以下尺寸都是"单个汉字的像素宽"，参照你们现有成片测量
    title_px = fit_size(top_title, 118 * s, max_w)
    sub_px = fit_size(subtitle, 62 * s, max_w)
    title_mv = int(60 * s)
    sub_mv = title_mv + int(title_px * 1.2)
    title_size, sub_size = title_px / tr, sub_px / tr

    def style(name, font, size, color, outline, align, mv, ml=0, mr=0):
        return (
            f"Style: {name},{font},{int(size)},{color},{color},{black},{black},"
            f"-1,0,0,0,100,100,0,0,1,{outline:.1f},0,{align},{ml},{mr},{mv},1"
        )

    styles = [
        style("Title", title_font, title_size, yellow, 7 * s, 8, title_mv),
        style("Sub", title_font, sub_size, yellow, 5 * s, 8, sub_mv),
        style("Caption", caption_font, 64 * s / cr, white, 6 * s, 2, int(height * 0.30)),
        style("Note", caption_font, 26 * s / cr, white, 2 * s, 1, int(24 * s), ml=int(28 * s)),
        style("Label", caption_font, 26 * s / cr, white, 2 * s, 3, int(24 * s), mr=int(28 * s)),
        style("Center", title_font, 110 * s / tr, yellow, 9 * s, 5, 0),
    ]
    end = _ts(duration + 1)
    events = []
    if top_title:
        events.append(f"Dialogue: 1,0:00:00.00,{end},Title,,0,0,0,,{_clean(top_title)}")
    if subtitle:
        events.append(f"Dialogue: 1,0:00:00.00,{end},Sub,,0,0,0,,{_clean(subtitle)}")
    for start, stop, text in captions:
        events.append(f"Dialogue: 0,{_ts(start)},{_ts(stop)},Caption,,0,0,0,,{text}")
    if render.get("disclaimer"):
        events.append(f"Dialogue: 0,0:00:00.00,{end},Note,,0,0,0,,{_clean(render['disclaimer'])}")
    if render.get("ai_label"):
        events.append(f"Dialogue: 0,0:00:00.00,{end},Label,,0,0,0,,{_clean(render['ai_label'])}")
    if center_text:
        # 封面大字：按空格分行，每行单独算字号
        lines = [p for p in center_text.split() if p]
        rendered = "\\N".join(
            "{\\fs" + str(int(fit_size(line, 110 * s, max_w) / tr)) + "}" + _clean(line) for line in lines
        )
        events.append(f"Dialogue: 2,0:00:00.00,{end},Center,,0,0,0,,{rendered}")

    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            *styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )
