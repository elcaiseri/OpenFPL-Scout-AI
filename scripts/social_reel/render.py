"""Draw the reel scenes, encode the MP4 and export the stills.

Design tokens mirror ``static/style.css`` (:root) and the squad pitch/cards,
so the reel reads as the same product as the site.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .data import POSITION_WORDS, Player, Squad, armband_line, caption, club_short, stacks, status_line

REPO_ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = REPO_ROOT / "assets" / "social" / "fonts"
LION_PATH = REPO_ROOT / "assets" / "openfpl-predictive-lion-frameless-2026.png"

W, H, FPS = 1080, 1920, 30
CAROUSEL_H = 1350

# ---- brand tokens (static/style.css :root) ----
INK950 = (11, 4, 16)
PLUM900 = (33, 6, 47)
PLUM800 = (53, 8, 73)
PLUM600 = (104, 32, 138)
LIME = (200, 255, 50)
AQUA = (57, 241, 255)
PINK = (255, 79, 167)
PAPER = (248, 247, 251)
MUTED = (184, 173, 195)
PITCH_D = (5, 107, 72)
PITCH_L = (7, 132, 86)
GREY_TXT = (97, 87, 106)
FIX_TXT = (116, 106, 123)

# Headline validation figures from the technical report (static/report).
# Update these when the report is re-run.
REPORT_STATS = (
    ("31/38", "GWs beat a\n5-GW form squad"),
    ("0.70", "rank correlation\n(Spearman ρ)"),
    ("1.92", "holdout RMSE\nper player-GW"),
)

FONT_FILES = {
    "disp": "Anton-Regular.ttf", "black": "Inter-Black.ttf", "xb": "Inter-ExtraBold.ttf",
    "b": "Inter-Bold.ttf", "sb": "Inter-SemiBold.ttf", "m": "Inter-Medium.ttf",
}


# ---- small helpers ----
def cl(x: float) -> float:
    return max(0.0, min(1.0, x))


def eo(x: float) -> float:
    x = cl(x)
    return 1 - (1 - x) ** 3


def eback(x: float) -> float:
    x = cl(x)
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def prog(t: float, start: float, dur: float) -> float:
    return cl((t - start) / dur)


def A(c: Tuple[int, int, int], a: float) -> Tuple[int, int, int, int]:
    return c + (int(255 * cl(a)),)


_fonts: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(FONT_DIR / FONT_FILES[weight]), size)
    return _fonts[key]


def fit(text: str, weight: str, size: int, max_width: float) -> ImageFont.FreeTypeFont:
    while size > 12 and font(weight, size).getlength(text) > max_width:
        size -= 1
    return font(weight, size)


def with_alpha(im: Image.Image, al: float) -> Image.Image:
    if al >= 0.999:
        return im
    im = im.copy()
    im.putalpha(im.getchannel("A").point(lambda v: int(v * cl(al))))
    return im


def paste(base: Image.Image, im: Image.Image, cx: float, cy: float, al: float = 1.0, scale: float = 1.0) -> None:
    if al <= 0.003:
        return
    if abs(scale - 1) > 0.005:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.BILINEAR)
    im = with_alpha(im, al)
    base.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2)))


def grad(w: int, h: int, c0, c1, angle: float = 155) -> Image.Image:
    a = math.radians(angle - 90)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    t = xx * math.cos(a) + yy * math.sin(a)
    t = (t - t.min()) / (t.max() - t.min() + 1e-6)
    arr = np.zeros((h, w, 4), np.uint8)
    for i in range(3):
        arr[..., i] = (c0[i] + (c1[i] - c0[i]) * t).astype(np.uint8)
    arr[..., 3] = 255
    return Image.fromarray(arr, "RGBA")


def rounded(im: Image.Image, r: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], r, fill=255)
    out = im.copy()
    out.putalpha(mask)
    return out


def shadowed(im: Image.Image, blur: int = 18, off: int = 10, op: int = 110) -> Image.Image:
    pad = blur * 2 + off
    out = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", im.size, (0, 18, 10, op))
    sh.putalpha(im.getchannel("A").point(lambda v: v * op // 255))
    out.alpha_composite(sh, (pad, pad + off))
    out = out.filter(ImageFilter.GaussianBlur(blur))
    out.alpha_composite(im, (pad, pad))
    return out


def disp(d: ImageDraw.ImageDraw, xy, text: str, size: int, fill, anchor: str = "mm", max_width: float = 0) -> None:
    f = fit(text, "disp", size, max_width) if max_width else font("disp", size)
    d.text(xy, text, font=f, fill=fill, anchor=anchor)


class Reel:
    """All scenes for one gameweek's squad."""

    def __init__(self, squad: Squad):
        self.sq = squad
        self.gw = squad.gameweek
        self.lion_src = Image.open(LION_PATH).convert("RGBA")
        self._lions: Dict[int, Image.Image] = {}
        self.bg = self._make_bg()
        self.cards = {p.name: self._make_card(p) for p in squad.players}
        self.pitch = self._make_pitch()
        self.labels = {k: self._pos_label(k) for k in ("FORWARDS", "MIDFIELDERS", "DEFENDERS", "GOALKEEPERS")}
        self.big_captain = self._make_big_captain()
        self.vice_row = self._make_vice_row()
        self.scenes: List[Tuple[str, Callable, float, float]] = [
            ("hook", self.s_hook, 0.0, 3.0),
            ("captain", self.s_captain, 3.0, 7.0),
            ("full15", self.s_pitch, 7.0, 15.4),
            ("top5", self.s_top, 15.4, 19.8),
            ("cta", self.s_outro, 19.8, 24.5),
        ]
        self.duration = self.scenes[-1][3]
        self.fade = 0.3
        self.bar_scenes = {"captain", "full15", "top5"}
        self.still_mode = False

    # ---- assets ----
    def lion(self, size: int) -> Image.Image:
        if size not in self._lions:
            self._lions[size] = self.lion_src.resize((size, size), Image.LANCZOS)
        return self._lions[size]

    def _make_bg(self) -> Image.Image:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        a = math.radians(115 - 90)
        t = xx * math.cos(a) + yy * math.sin(a)
        t = (t - t.min()) / (t.max() - t.min())
        stops = [(0.0, (16, 4, 23)), (0.04, (16, 4, 23)), (0.64, (47, 8, 65)), (1.0, (7, 46, 49))]
        arr = np.zeros((H, W, 3), np.float32)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            m = (t >= p0) & (t <= p1)
            k = ((t - p0) / max(p1 - p0, 1e-6))[m]
            for i in range(3):
                arr[..., i][m] = c0[i] + (c1[i] - c0[i]) * k
        d = np.sqrt(((xx - W / 2) / W) ** 2 + ((yy - H * 0.45) / H) ** 2)
        arr *= (1 - 0.35 * np.clip(d * 1.4, 0, 1))[..., None]
        bg = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")
        # Outlined, skewed gameweek number, like the site header's "26".
        wm = Image.new("RGBA", (W + 400, 1100), (0, 0, 0, 0))
        ImageDraw.Draw(wm).text((W + 380, 1080), f"{self.gw:02d}", font=font("disp", 1000), anchor="rd",
                                fill=(0, 0, 0, 0), stroke_width=3, stroke_fill=(200, 255, 50, 34))
        wm = wm.transform(wm.size, Image.AFFINE, (1, 0.14, -150, 0, 1, 0), Image.BICUBIC)
        bg.alpha_composite(wm, (-260, H - 1100 + 140))
        return bg

    @staticmethod
    def _venue_chip(d: ImageDraw.ImageDraw, x0: float, cy: float, size: int, venue: str, text: str,
                    text_font, text_fill) -> None:
        chip_font = font("black", int(size * 0.55))
        cw = max(size, chip_font.getlength(venue) + size * 0.5)
        d.rounded_rectangle([x0, cy - size / 2, x0 + cw, cy + size / 2], int(size * 0.25), fill=A(PLUM900, 1))
        d.text((x0 + cw / 2, cy), venue, font=chip_font, fill=A(PAPER, 1), anchor="mm")
        d.text((x0 + cw + size * 0.25, cy), text, font=text_font, fill=text_fill, anchor="lm")

    @staticmethod
    def _chip_width(size: int, venue: str, text: str, text_font) -> float:
        chip_font = font("black", int(size * 0.55))
        cw = max(size, chip_font.getlength(venue) + size * 0.5)
        return cw + size * 0.25 + text_font.getlength(text)

    @staticmethod
    def xp_pill(v: float, size: int = 30) -> Image.Image:
        f = font("black", size)
        fs = font("black", int(size * 0.6))
        tw = f.getlength(f"{v:.1f}") + 6 + fs.getlength("xP")
        w, h = int(tw + 30), int(size * 1.55)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, w - 1, h - 1], int(h * 0.3), fill=A(PLUM900, 1))
        d.text((15, h / 2), f"{v:.1f}", font=f, fill=A(PAPER, 1), anchor="lm")
        d.text((15 + f.getlength(f"{v:.1f}") + 6, h / 2 + size * 0.12), "xP", font=fs, fill=A(LIME, 1), anchor="lm")
        return im

    @staticmethod
    def role_badge(role: str, size: int = 48) -> Image.Image:
        im = Image.new("RGBA", (size + 8, size + 8), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([2, 2, size + 5, size + 5], int(size * 0.3), fill=A(LIME if role == "C" else AQUA, 1),
                            outline=A(PITCH_D, 1), width=4)
        d.text(((size + 8) / 2, (size + 8) / 2), role, font=font("black", int(size * 0.5)), fill=A(INK950, 1),
               anchor="mm")
        return im.rotate(-5, resample=Image.BICUBIC, expand=True)

    def _make_card(self, p: Player) -> Image.Image:
        cw, ch = 186, 186
        role = p.role
        c0, c1 = ((247, 255, 214), (223, 255, 130)) if role == "C" else \
            ((244, 254, 255), (186, 249, 255)) if role == "V" else ((255, 255, 255), (233, 240, 235))
        body = rounded(grad(cw, ch, c0, c1), 22)
        d = ImageDraw.Draw(body)
        edge = LIME if role == "C" else AQUA if role == "V" else (255, 255, 255)
        d.rounded_rectangle([0, 0, cw - 1, ch - 1], 22, outline=A(edge, 1), width=3 if role else 1)
        d.rounded_rectangle([cw * 0.15, 0, cw * 0.85, 5], 3, fill=A(PLUM600, 1))
        d.text((cw / 2, 30), p.position, font=font("black", 15), fill=A(PLUM600, 1), anchor="mm")
        d.text((cw / 2, 62), p.name, font=fit(p.name, "black", 30, cw - 20), fill=A(INK950, 1), anchor="mm")
        d.text((cw / 2, 92), p.club_short, font=fit(p.club_short, "b", 19, cw - 20), fill=A(GREY_TXT, 1), anchor="mm")
        opp = p.opponent_codes
        ft = fit(opp, "b", 19, cw - 60)
        tw = self._chip_width(24, p.venue, opp, ft)
        self._venue_chip(d, cw / 2 - tw / 2, 119, 24, p.venue, opp, ft, A(FIX_TXT, 1))
        pill = self.xp_pill(p.xp, 24)
        body.alpha_composite(pill, (int(cw / 2 - pill.width / 2), ch - pill.height - 12))
        canvas = Image.new("RGBA", (cw + 40, ch + 40), (0, 0, 0, 0))
        canvas.alpha_composite(body, (16, 24))
        if role:
            b = self.role_badge(role, 42)
            canvas.alpha_composite(b, (cw + 40 - b.width, 0))
        return shadowed(canvas, blur=12, off=8, op=95)

    def _make_pitch(self) -> Image.Image:
        pw, ph = W - 80, 1120
        im = Image.new("RGBA", (pw, ph), A(PITCH_D, 1))
        d = ImageDraw.Draw(im)
        for i in range(10):
            if i % 2:
                d.rectangle([0, i * ph / 10, pw, (i + 1) * ph / 10], fill=A(PITCH_L, 1))
        line = (255, 255, 255, 70)
        d.rounded_rectangle([18, 18, pw - 18, ph - 18], 8, outline=line, width=3)
        d.line([18, ph / 2, pw - 18, ph / 2], fill=line, width=3)
        d.ellipse([pw / 2 - 120, ph / 2 - 120, pw / 2 + 120, ph / 2 + 120], outline=line, width=3)
        d.rectangle([pw / 2 - 230, 18, pw / 2 + 230, 150], outline=line, width=3)
        d.rectangle([pw / 2 - 230, ph - 150, pw / 2 + 230, ph - 18], outline=line, width=3)
        sh = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(sh).rectangle([0, 0, pw, ph], outline=255, width=60)
        sh = sh.filter(ImageFilter.GaussianBlur(40)).point(lambda v: v * 80 // 255)
        dark = Image.new("RGBA", (pw, ph), (0, 24, 12, 255))
        dark.putalpha(sh)
        im.alpha_composite(dark)
        im = rounded(im, 30)
        ImageDraw.Draw(im).rounded_rectangle([0, 0, pw - 1, ph - 1], 30, outline=(255, 255, 255, 50), width=2)
        return im

    @staticmethod
    def _pos_label(text: str) -> Image.Image:
        f = font("black", 18)
        w = int(sum(f.getlength(ch) + 2.9 for ch in text) + 34)
        h = 38
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, w - 1, h - 1], 19, fill=(0, 31, 21, 140), outline=(255, 255, 255, 45), width=2)
        x = 18
        for ch in text:
            d.text((x, h / 2), ch, font=f, fill=(255, 255, 255, 200), anchor="lm")
            x += f.getlength(ch) + 2.9
        return im

    def _make_big_captain(self) -> Image.Image:
        c = self.sq.captain
        w, h = 880, 640
        body = rounded(grad(w, h, (247, 255, 214), (223, 255, 130)), 44)
        d = ImageDraw.Draw(body)
        d.rounded_rectangle([w * 0.2, 0, w * 0.8, 9], 5, fill=A(PLUM600, 1))
        d.text((w / 2, 70), f"{POSITION_WORDS[c.position]}  ·  CAPTAIN", font=font("black", 26),
               fill=A(PLUM600, 1), anchor="mm")
        disp(d, (w / 2, 200), c.name.upper(), 190, A(INK950, 1), max_width=w - 100)
        d.text((w / 2, 320), c.club_short, font=font("xb", 40), fill=A(GREY_TXT, 1), anchor="mm")
        opp = c.opponent_codes
        ft = font("b", 34)
        tw = self._chip_width(44, c.venue, opp, ft)
        self._venue_chip(d, w / 2 - tw / 2, 380, 44, c.venue, opp, ft, A(FIX_TXT, 1))
        d.rounded_rectangle([w / 2 - 230, 440, w / 2 + 230, 600], 34, fill=A(PLUM900, 1))
        return body

    def _make_vice_row(self) -> Image.Image:
        v = self.sq.vice
        vc = rounded(grad(860, 170, (244, 254, 255), (186, 249, 255)), 34)
        vd = ImageDraw.Draw(vc)
        vd.rounded_rectangle([40, 45, 120, 125], 22, fill=A(AQUA, 1), outline=A(PITCH_D, 1), width=4)
        vd.text((80, 85), "V", font=font("black", 44), fill=A(INK950, 1), anchor="mm")
        pill = self.xp_pill(v.xp, 40)
        name_w = 860 - 150 - pill.width - 60
        disp(vd, (150, 62), v.name.upper(), 64, A(INK950, 1), anchor="lm", max_width=name_w)
        vd.text((150, 122), f"{v.club_short}  ·  {v.fixture}", font=fit(f"{v.club_short}  ·  {v.fixture}", "b", 28, name_w),
                fill=A(GREY_TXT, 1), anchor="lm")
        vc.alpha_composite(pill, (860 - pill.width - 36, 85 - pill.height // 2))
        return shadowed(vc, 18, 10, 110)

    # ---- chrome ----
    def brand_bar(self, base: Image.Image, al: float) -> None:
        if al <= 0:
            return
        paste(base, self.lion(96), 112, 160, al)
        d = ImageDraw.Draw(base)
        d.text((170, 132), "OpenFPL", font=font("xb", 36), fill=A(PAPER, al))
        d.text((171, 174), "SCOUT AI", font=font("black", 17), fill=A(LIME, al))
        f = font("black", 20)
        txt = f"GAMEWEEK {self.gw}"
        w = f.getlength(txt) + 44
        d.rounded_rectangle([W - 60 - w, 136, W - 60, 184], 24, fill=A(LIME, al))
        d.text((W - 60 - w / 2, 160), txt, font=f, fill=A(INK950, al), anchor="mm")

    # ---- scenes ----
    def s_hook(self, base: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(base)
        pl = eback(prog(t, 0.0, 0.7))
        glow = Image.new("RGBA", (700, 700), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse([150, 150, 550, 550], fill=(200, 255, 50, 70))
        paste(base, glow.filter(ImageFilter.GaussianBlur(80)), W / 2, 560, cl(pl))
        paste(base, self.lion(440), W / 2, 560, cl(pl * 1.4), scale=0.6 + 0.4 * pl)
        p1, p2, p3 = eo(prog(t, 0.45, 0.5)), eo(prog(t, 0.65, 0.5)), eo(prog(t, 1.0, 0.5))
        disp(d, (W / 2, 1000 + 50 * (1 - p1)), f"GAMEWEEK {self.gw}", 190, A(PAPER, p1), max_width=980)
        disp(d, (W / 2, 1180 + 50 * (1 - p2)), "AI SCOUT TEAM", 130, A(LIME, p2))
        lw = 560 * eo(prog(t, 0.9, 0.6))
        d.rectangle([W / 2 - lw / 2, 1282, W / 2 + lw / 2, 1290], fill=A(PINK, 1))
        d.text((W / 2, 1360), "15 picks  ·  captain  ·  vice-captain", font=font("sb", 36), fill=A(PAPER, p3),
               anchor="mm")
        pill = status_line(self.sq)
        pf = fit(pill, "b", 28, 900)
        pw = pf.getlength(pill) + 64
        d.rounded_rectangle([W / 2 - pw / 2, 1420, W / 2 + pw / 2, 1482], 31, fill=A(PLUM800, p3),
                            outline=A(PLUM600, p3), width=2)
        d.text((W / 2, 1451), pill, font=pf, fill=A(MUTED, p3), anchor="mm")

    def s_captain(self, base: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(base)
        p = eo(prog(t, 0.0, 0.45))
        disp(d, (W / 2, 330), "THE ARMBAND", 84, A(PAPER, p))
        pc = eback(prog(t, 0.2, 0.6))
        cx, cy = W / 2, 760
        card = self.big_captain.copy()
        cd = ImageDraw.Draw(card)
        v = self.sq.captain.xp * eo(prog(t, 0.6, 1.0))
        cd.text((420, 520), f"{v:.1f}", font=font("disp", 120), fill=A(PAPER, 1), anchor="mm")
        cd.text((552, 545), "xP", font=font("black", 40), fill=A(LIME, 1), anchor="mm")
        paste(base, shadowed(card, 24, 14, 120), cx, cy, cl(pc * 1.5), scale=0.85 + 0.15 * pc)
        paste(base, self.role_badge("C", 110), cx + 400, cy - 300, cl(prog(t, 0.6, 0.3)),
              scale=0.5 + 0.5 * eback(prog(t, 0.6, 0.5)))
        pv = eo(prog(t, 1.4, 0.5))
        paste(base, self.vice_row, W / 2, 1285 + 40 * (1 - pv), pv)
        pn = eo(prog(t, 2.1, 0.5))
        d.text((W / 2, 1450), armband_line(self.sq), font=font("xb", 38), fill=A(PINK, pn), anchor="mm")

    def s_pitch(self, base: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(base)
        p = eo(prog(t, 0.0, 0.5))
        disp(d, (W / 2, 300), "THE FULL 15", 96, A(PAPER, p))
        d.text((W / 2, 378), "2 GK · 5 DEF · 5 MID · 3 FWD  ·  max 3 per club", font=font("sb", 28),
               fill=A(MUTED, p), anchor="mm")
        pp = eo(prog(t, 0.1, 0.5))
        paste(base, self.pitch, W / 2, 1000 + 40 * (1 - pp), pp)
        rows = [("FORWARDS", "FWD"), ("MIDFIELDERS", "MID"), ("DEFENDERS", "DEF"), ("GOALKEEPERS", "GK")]
        ys = [600, 870, 1140, 1410]
        order = [3, 2, 1, 0]  # build from the back: GK, DEF, MID, FWD
        for ri, (label, pos) in enumerate(rows):
            start = 0.45 + order.index(ri) * 0.6
            paste(base, self.labels[label], W / 2, ys[ri] - 128, eo(prog(t, start - 0.1, 0.35)))
            players = self.sq.by_position(pos)
            n = len(players)
            gap = 198 if n == 5 else 236
            for i, pl in enumerate(players):
                q = prog(t, start + i * 0.08, 0.5)
                if q <= 0:
                    continue
                paste(base, self.cards[pl.name], W / 2 + (i - (n - 1) / 2) * gap, ys[ri] + 26 * (1 - eo(q)),
                      cl(q * 1.6), scale=0.55 + 0.45 * eback(q))

    def s_top(self, base: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(base)
        p = eo(prog(t, 0.0, 0.45))
        disp(d, (W / 2, 320), "TOP 5 BY xP", 104, A(PAPER, p))
        top = self.sq.top(5)
        scale_max = max(7.5, top[0].xp * 1.08)
        for i, pl in enumerate(top):
            q = eo(prog(t, 0.3 + i * 0.18, 0.7))
            al = cl(q * 2)
            y = 480 + i * 148
            disp(d, (80, y + 6), f"{i + 1}", 92, A(PINK, al), anchor="lm")
            d.text((160, y - 22), pl.name, font=fit(pl.name, "black", 42, 700), fill=A(PAPER, al), anchor="lm")
            sub = f"{pl.club_short}  ·  {pl.position}  ·  {pl.fixture}"
            d.text((160, y + 22), sub, font=fit(sub, "sb", 24, 700), fill=A(MUTED, al), anchor="lm")
            bw = 740
            d.rounded_rectangle([160, y + 50, 160 + bw, y + 66], 8, fill=A(PLUM800, al))
            col = LIME if pl.role == "C" else AQUA if pl.role == "V" else PAPER
            d.rounded_rectangle([160, y + 50, 160 + max(16, bw * (pl.xp / scale_max) * q), y + 66], 8, fill=A(col, al))
            disp(d, (W - 70, y + 4), f"{pl.xp * q:.1f}", 78, A(col, al), anchor="rm")
        groups = stacks(self.sq)
        if not groups:
            return
        ps = eo(prog(t, 1.6, 0.5))
        sy = 1250
        d.text((W / 2, sy), "STACKS THE MODEL LIKES", font=font("black", 28), fill=A(LIME, ps), anchor="mm")
        for j, (club, players) in enumerate(groups):
            y = sy + 44 + j * 124
            lead = players[0]
            d.rounded_rectangle([70, y, W - 70, y + 104], 26, fill=A(PLUM800, ps), outline=A(PLUM600, ps), width=2)
            disp(d, (110, y + 52), f"{len(players)}× {lead.club_code}", 58, A(LIME, ps), anchor="lm")
            top_line = f"{club_short(club)} · {lead.fixture}"
            names = " · ".join(p.name for p in players)
            d.text((340, y + 34), top_line, font=fit(top_line, "black", 27, 640), fill=A(PAPER, ps), anchor="lm")
            d.text((340, y + 72), names, font=fit(names, "sb", 24, 640), fill=A(MUTED, ps), anchor="lm")

    def s_outro(self, base: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(base)
        p0 = eback(prog(t, 0.0, 0.6))
        paste(base, self.lion(300), W / 2, 380, cl(p0 * 1.4), scale=0.6 + 0.4 * p0)
        p1, p2, p3 = eo(prog(t, 0.2, 0.5)), eo(prog(t, 0.5, 0.5)), eo(prog(t, 1.0, 0.5))
        disp(d, (W / 2, 610), "READ THE GAME.", 92, A(PAPER, p1))
        disp(d, (W / 2, 712), "WIN THE GAMEWEEK.", 92, A(LIME, p1))
        d.text((W / 2, 800), "Ridge · XGBoost · CatBoost · MLP ensemble", font=font("sb", 30), fill=A(MUTED, p1),
               anchor="mm")
        for i, (value, label) in enumerate(REPORT_STATS):
            cx = W / 2 + (i - 1) * 312
            q = eo(prog(t, 0.5 + i * 0.12, 0.5))
            d.rounded_rectangle([cx - 146, 870, cx + 146, 1110], 28, fill=A(PLUM800, q), outline=A(PLUM600, q), width=2)
            disp(d, (cx, 950), value, 84, A(LIME, q))
            d.multiline_text((cx, 1040), label, font=font("sb", 23), fill=A(MUTED, q), anchor="mm", align="center",
                             spacing=6)
        d.text((W / 2, 1160), "Validated on a full held-out Premier League season", font=font("sb", 27),
               fill=A(PAPER, p2), anchor="mm")
        pulse = 1.0 if self.still_mode else 1 + 0.025 * math.sin(t * 6)
        bw, bh = 840 * pulse, 136 * pulse
        d.rounded_rectangle([W / 2 - bw / 2, 1290 - bh / 2, W / 2 + bw / 2, 1290 + bh / 2], 68, fill=A(LIME, p3))
        d.text((W / 2, 1290), "openfpl.kassem.dev", font=font("black", 62), fill=A(INK950, p3), anchor="mm")
        d.text((W / 2, 1410), "Free xP for every player  ·  Rate your team /100", font=font("sb", 29),
               fill=A(PAPER, p3), anchor="mm")
        d.text((W / 2, 1462), "Open source on GitHub  ·  @elcaiseri", font=font("m", 27), fill=A(MUTED, p3),
               anchor="mm")

    # ---- composition ----
    def frame(self, T: float, progress: bool = True) -> Image.Image:
        img = self.bg.copy()
        bar = 0.0
        for name, fn, a, b in self.scenes:
            if not (a - 1e-3 <= T < b + self.fade):
                continue
            fin = cl((T - a) / self.fade) if a > 0 else 1.0
            fout = cl((b - T) / self.fade) if b < self.duration else 1.0
            al = min(fin, fout) if T < b else 0.0
            if al <= 0:
                continue
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            fn(layer, T - a)
            img.alpha_composite(with_alpha(layer, al))
            if name in self.bar_scenes:
                bar = max(bar, al)
        self.brand_bar(img, bar)
        if progress:
            ImageDraw.Draw(img).rectangle([0, H - 8, W * T / self.duration, H], fill=A(LIME, 1))
        return img.convert("RGB")

    def still_times(self) -> List[Tuple[str, float]]:
        """A moment in each scene after every element has landed, before it fades."""
        return [(name, b - self.fade - 0.05) for name, _, _, b in self.scenes[:-1]] + \
               [(self.scenes[-1][0], self.duration - 0.05)]

    def carousel(self, still: Image.Image, index: int, total: int) -> Image.Image:
        band = still.crop((0, 100, W, 1600))
        band = band.resize((int(W * CAROUSEL_H / band.height), CAROUSEL_H), Image.LANCZOS)
        out = self.bg.convert("RGB").crop((0, 285, W, 285 + CAROUSEL_H))
        out.paste(band, ((W - band.width) // 2, 0))
        d = ImageDraw.Draw(out)
        txt = f"{index}/{total}"
        f = font("black", 22)
        w = f.getlength(txt) + 28
        d.rounded_rectangle([W - 24 - w, CAROUSEL_H - 62, W - 24, CAROUSEL_H - 24], 19, fill=PLUM800,
                            outline=PLUM600, width=2)
        d.text((W - 24 - w / 2, CAROUSEL_H - 43), txt, font=f, fill=PAPER, anchor="mm")
        return out


def export(reel: "Reel", out_dir: Path, stem: str, caption_text: str, video: bool = True) -> Dict[str, List[Path]]:
    """Write a reel's stills (9:16 and 4:5), a zip of them, the caption and optionally the MP4."""
    out_dir.mkdir(parents=True, exist_ok=True)
    made: Dict[str, List[Path]] = {"video": [], "stills_4x5": [], "stills_9x16": [], "zip": [], "caption": []}

    reel.still_mode = True
    times = reel.still_times()
    for i, (name, T) in enumerate(times, 1):
        still = reel.frame(T, progress=False)
        tall = out_dir / f"{stem}_{i:02d}_{name}_9x16.png"
        still.save(tall, optimize=True)
        made["stills_9x16"].append(tall)
        square = out_dir / f"{stem}_{i:02d}_{name}_4x5.png"
        reel.carousel(still, i, len(times)).save(square, optimize=True)
        made["stills_4x5"].append(square)
    reel.still_mode = False

    zip_path = out_dir / f"{stem}_stills.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in made["stills_4x5"] + made["stills_9x16"]:
            zf.write(path, path.name)
    made["zip"].append(zip_path)

    caption_path = out_dir / f"{stem}_caption.txt"
    caption_path.write_text(caption_text, encoding="utf-8")
    made["caption"].append(caption_path)

    if video:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is not on PATH; install it or pass --no-video.")
        video_path = out_dir / f"{stem}_reel.mp4"
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
               "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-profile:v", "high",
               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(video_path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        assert proc.stdin is not None
        for i in range(int(reel.duration * FPS)):
            proc.stdin.write(reel.frame(i / FPS).tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg failed to encode the reel.")
        made["video"].append(video_path)
    return made


def build(squad: Squad, out_dir: Path, video: bool = True) -> Dict[str, List[Path]]:
    """Write the preview reel, both still sets, a zip of the stills and a caption."""
    return export(Reel(squad), out_dir, f"openfpl_gw{squad.gameweek:02d}", caption(squad), video=video)
