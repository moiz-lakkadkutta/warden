"""Timeline GIF: runs/<id>/events.jsonl → an animated replay of the five phases.

Pillow draws every frame directly (no browser, no screen recorder), so the GIF is reproducible
from the run log alone. Pillow is the optional `gif` extra (`uv sync --extra gif`); it is imported
only inside `render_gif`. Layout (`segments`, `position`, `frame_states`) is pure and tested
without drawing. The GIF uses the console's dark palette: a GIF cannot follow the viewer's theme,
and the dark ground keeps all five phase colours legible.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from warden.runlog.events import phase_spans
from warden.runlog.render import (
    PHASE_COLOURS,
    PHASES,
    _fmt_ms,
    event_text,
    phase_durations_ms,
)
from warden.schemas import Phase, RunEvent

if TYPE_CHECKING:
    from PIL import Image, ImageDraw, ImageFont

W, H = 960, 420
PAD = 32
BAR_Y, BAR_H = 108, 44
LOG_LINES = 6
MIN_SHARE = 0.07  # every phase keeps a readable sliver, even with no events

BG = "#14181D"
CARD = "#1E242B"
INK = "#E8ECF0"
MUTED = "#A7B0BA"
LINE = "#333B45"
# dark-ground variants of render.VERDICT_COLOURS (same hues the console uses in dark mode)
VERDICT_DARK: dict[str, str] = {
    "PASS": "#4CC38A",
    "PASS WITH CONCERNS": "#E0A03A",
    "FAIL": "#F0655B",
    "PENDING-HUMAN": "#E0A03A",
    "COST CAP": "#F0655B",
    "RUNNING": "#7B8592",
}


class GifUnavailableError(RuntimeError):
    """Pillow is not installed; the console page still renders without the GIF."""

    fix = "uv sync --extra gif   (or: pip install 'warden[gif]')"


# --- layout (pure) ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    phase: Phase
    x0: float  # fraction of the bar, 0..1
    x1: float
    start: datetime | None
    end: datetime | None
    ms: int


def segments(events: list[RunEvent]) -> list[Segment]:
    """The five phases left to right; widths = a floor share + the rest scaled by duration."""
    durs = phase_durations_ms(events)
    spans = phase_spans(events)
    total = sum(durs.values())
    free = 1.0 - MIN_SHARE * len(PHASES)
    out: list[Segment] = []
    x = 0.0
    for p in PHASES:
        share = durs[p] / total if total else 1.0 / len(PHASES)
        w = MIN_SHARE + free * share
        start, end = spans.get(p, (None, None))
        out.append(Segment(p, x, x + w, start, end, durs[p]))
        x += w
    return out


def position(ev: RunEvent, segs: list[Segment]) -> float:
    """Where on the bar (0..1) the event finishes: its phase segment, by time within the phase."""
    seg = next(s for s in segs if s.phase == ev.phase)
    if seg.start is None or seg.end is None or seg.end <= seg.start:
        return seg.x1
    done = ev.ts.timestamp() + (ev.ms or 0) / 1000
    frac = (done - seg.start.timestamp()) / (seg.end - seg.start).total_seconds()
    return seg.x0 + max(0.0, min(1.0, frac)) * (seg.x1 - seg.x0)


@dataclass(frozen=True)
class FrameState:
    head: float  # playhead, 0..1
    seen: list[RunEvent]  # events finished by the playhead, in bar order
    usd: float
    elapsed_ms: int  # phase time covered by the playhead (idle gaps between phases excluded)
    phase_usd: dict[str, float]


def frame_states(events: list[RunEvent], frames: int) -> list[FrameState]:
    segs = segments(events)
    placed = sorted(((position(ev, segs), i, ev) for i, ev in enumerate(events)))
    out: list[FrameState] = []
    n = max(2, frames)
    for k in range(n):
        head = k / (n - 1)
        seen = [ev for pos, _, ev in placed if pos <= head + 1e-9]
        phase_usd: dict[str, float] = {p: 0.0 for p in PHASES}
        for ev in seen:
            phase_usd[ev.phase] += ev.usd or 0.0
        elapsed = 0
        for s in segs:
            if head >= s.x1:
                elapsed += s.ms
            elif head > s.x0:
                elapsed += int(s.ms * (head - s.x0) / (s.x1 - s.x0))
        out.append(FrameState(head, seen, sum(phase_usd.values()), elapsed, phase_usd))
    return out


# --- drawing ---------------------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    from PIL import ImageFont

    for name in ("DejaVuSansMono.ttf", "Menlo.ttc", "SFNSMono.ttf", "consola.ttf"):
        for path in (name, f"/System/Library/Fonts/{name}"):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> tuple[int, int, int]:
    ra, rb = _rgb(a), _rgb(b)
    return (
        round(ra[0] + (rb[0] - ra[0]) * t),
        round(ra[1] + (rb[1] - ra[1]) * t),
        round(ra[2] + (rb[2] - ra[2]) * t),
    )


def _fit(draw: ImageDraw.ImageDraw, text: str, font: Any, width: int) -> str:
    text = text.replace("→", "->")
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "..."


def _clock(ms: int) -> str:
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _draw_frame(
    st: FrameState,
    segs: list[Segment],
    *,
    run_id: str,
    title: str | None,
    verdict: str | None,
    counts: dict[str, int],
) -> Image.Image:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    big, mid, small = _font(22), _font(15), _font(13)
    span = W - 2 * PAD

    # header: product · run id, issue title, verdict pill once the playhead reaches the end
    d.text((PAD, 24), "warden", fill=INK, font=big)
    d.text((PAD + d.textlength("warden ", font=big), 30), f"run {run_id}", fill=MUTED, font=mid)
    if title:
        d.text((PAD, 60), _fit(d, title, mid, span - 220), fill=MUTED, font=mid)
    if verdict is not None:
        colour = VERDICT_DARK.get(verdict, VERDICT_DARK["RUNNING"])
        tw = d.textlength(verdict, font=mid)
        x1 = W - PAD
        d.rounded_rectangle((x1 - tw - 28, 22, x1, 50), radius=14, fill=colour)
        d.text((x1 - tw - 14, 27), verdict, fill=BG, font=mid)

    # phase bar: dim track, filled up to the playhead
    for s in segs:
        a, b = PAD + s.x0 * span + 2, PAD + s.x1 * span - 2
        colour = PHASE_COLOURS[s.phase]
        d.rounded_rectangle((a, BAR_Y, b, BAR_Y + BAR_H), radius=4, fill=_mix(BG, colour, 0.35))
        fill_to = min(b, PAD + st.head * span)
        if fill_to > a and s.start is not None:  # a phase with no events stays a dim track
            d.rounded_rectangle((a, BAR_Y, fill_to, BAR_Y + BAR_H), radius=4, fill=colour)
        d.text((a + 8, BAR_Y + 6), _fit(d, s.phase, small, int(b - a - 12)), fill=INK, font=small)
        cost = f"${st.phase_usd[s.phase]:.2f}"
        d.text((a + 8, BAR_Y + 24), _fit(d, cost, small, int(b - a - 12)), fill=INK, font=small)
        dur = _fit(d, _fmt_ms(s.ms), small, int(b - a))
        d.text((a, BAR_Y + BAR_H + 6), dur, fill=MUTED, font=small)
    hx = PAD + st.head * span
    d.line((hx, BAR_Y - 8, hx, BAR_Y + BAR_H + 4), fill=INK, width=2)

    # running totals
    seen_counts = {k: sum(1 for ev in st.seen if ev.kind == k) for k in counts}
    stats = f"elapsed {_clock(st.elapsed_ms)}    cost ${st.usd:.2f}    " + "    ".join(
        f"{k} {seen_counts[k]}/{counts[k]}" for k in counts
    )
    d.text((PAD, 196), _fit(d, stats, mid, span), fill=INK, font=mid)

    # the last few events, newest at the bottom, each marked with its phase colour
    box_top = 228
    d.rounded_rectangle((PAD, box_top, W - PAD, H - 20), radius=6, fill=CARD, outline=LINE)
    tail = st.seen[-LOG_LINES:]
    for i, ev in enumerate(tail):
        y = box_top + 14 + i * 27
        d.rectangle((PAD + 14, y + 3, PAD + 24, y + 13), fill=PHASE_COLOURS[ev.phase])
        k = ev.data.get("candidate")
        tag = f"#{k} " if isinstance(k, int) else ""  # best-of-N: which candidate's line
        line = _fit(d, f"{ev.phase:<6} {tag}{event_text(ev)}", small, span - 56)
        d.text((PAD + 34, y), line, fill=INK if ev is tail[-1] else MUTED, font=small)
    if not tail:
        d.text((PAD + 14, box_top + 14), "waiting for the first event...", fill=MUTED, font=small)
    return img


def render_gif(
    events: list[RunEvent],
    out: Path,
    *,
    run_id: str,
    title: str | None = None,
    verdict: str | None = None,
    frames: int = 48,
    frame_ms: int = 90,
    hold_ms: int = 3000,
) -> Path:
    """Write an animated GIF of the run's timeline to `out`; the last frame holds the verdict."""
    try:
        from PIL import Image
    except ImportError as exc:  # optional extra
        raise GifUnavailableError("timeline.gif needs Pillow") from exc

    segs = segments(events)
    states = frame_states(events, frames)
    counts = {k: sum(1 for ev in events if ev.kind == k) for k in ("llm", "sandbox", "tool")}
    rgb = [
        _draw_frame(
            st,
            segs,
            run_id=run_id,
            title=title,
            verdict=verdict if i == len(states) - 1 else None,
            counts=counts,
        )
        for i, st in enumerate(states)
    ]
    # one shared palette (first + last frame cover every colour) so frames do not flicker
    sample = Image.new("RGB", (W, H * 2))
    sample.paste(rgb[0], (0, 0))
    sample.paste(rgb[-1], (0, H))
    palette = sample.quantize(colors=96, method=Image.Quantize.MEDIANCUT)
    pal = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in rgb]
    durations = [frame_ms] * (len(pal) - 1) + [hold_ms]
    out.parent.mkdir(parents=True, exist_ok=True)
    pal[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=pal[1:],
        duration=durations,
        loop=0,
        disposal=1,
    )
    return out
