"""ASS subtitle generation: word-level karaoke highlight, dead-centre.

The active word is yellow, pending words white — this is the classic ASS
karaoke trick (PrimaryColour = active, SecondaryColour = pending, \\k timings).
"""

from __future__ import annotations

from .tts import Word

ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Noto Sans,76,&H0000E5FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

MAX_WORDS_PER_CARD = 4
MAX_CARD_SECONDS = 1.6


def _ts(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _cardify(words: list[Word]) -> list[tuple[Word, Word, list[Word]]]:
    """Group words into caption cards: (first, last, group)."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        span = (w.end - cur[0].start) if cur else 0
        if len(cur) >= MAX_WORDS_PER_CARD or span >= MAX_CARD_SECONDS:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [(g[0], g[-1], g) for g in groups]


def build_ass(words: list[Word], out_path: str) -> str:
    """Write an .ass file covering `words` (scene-local timestamps)."""
    lines = [ASS_HEADER]
    for first, last, group in _cardify(words):
        parts: list[str] = []
        prev_end = first.start
        for w in group:
            gap_cs = max(int(round((w.start - prev_end) * 100)), 0)
            hold_cs = max(int(round((w.end - w.start) * 100)), 1)
            if parts:
                parts.append(f"{{\\k{gap_cs}}}")
            else:
                # pad the very first word so the card appears with it
                pad = max(int(round(first.start * 100)), 0)
                if pad:
                    parts.append(f"{{\\k{pad}}}")
            parts.append(f"{{\\k{hold_cs}}}{w.text} ")
            prev_end = w.end
        text = "".join(parts).rstrip()
        # pad card end so it does not flicker between words
        start = max(first.start - 0.05, 0.0)
        end = last.end + 0.20
        line = (
            f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{text}"
        )
        lines.append(line)
    content = "\n".join(lines) + "\n"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return out_path
