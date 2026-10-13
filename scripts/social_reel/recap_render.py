"""Post-gameweek recap reel: the locked forecast against actual points.

Reuses the preview reel's brand, chrome and pitch; swaps the scenes for
hook → score → armband → full 15 with points → top hauls and misses → CTA.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List

from PIL import Image, ImageDraw, ImageFilter

from .data import POSITION_WORDS, SITE_URL, Player
from .recap import Recap, recap_caption
from .render import (A, AQUA, FIX_TXT, GREY_TXT, INK950, LIME, MUTED, PAPER, PINK, PITCH_D, PLUM600, PLUM800,
                     PLUM900, REPORT_STATS, W, Reel, cl, disp, eback, eo, export, fit, font, grad, paste, prog,
                     rounded, shadowed, with_alpha)

BEAT = (12, 150, 90)


class RecapReel(Reel):
    MODE = "RECAP"

    def __init__(self, recap: Recap):
        # Set before Reel.__init__, which builds the cards through the overrides below.
        self.rc = recap
        super().__init__(recap.squad)
        self.scenes = [
            ("hook", self.s_hook, 0.0, 3.0),
            ("score", self.s_score, 3.0, 7.6),
            ("armband", self.s_captain, 7.6, 11.6),
            ("full15", self.s_pitch, 11.6, 20.0),
            ("hauls", self.s_top, 20.0, 24.6),
            ("cta", self.s_outro, 24.6, 29.0),
        ]
        self.duration = self.scenes[-1][3]
        self.bar_scenes = {"score", "armband", "full15", "hauls"}

    @staticmethod
    def pts_pill(pts: int, size: int = 30) -> Image.Image:
        label = "PT" if pts == 1 else "PTS"
        f, fs = font("black", size), font("black", int(size * 0.55))
        tw = f.getlength(str(pts)) + 6 + fs.getlength(label)
        w, h = int(tw + 30), int(size * 1.55)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, w - 1, h - 1], int(h * 0.3), fill=A(PLUM900, 1))
        d.text((15, h / 2), str(pts), font=f, fill=A(PAPER, 1), anchor="lm")
        d.text((15 + f.getlength(str(pts)) + 6, h / 2 + size * 0.1), label, font=fs, fill=A(LIME, 1), anchor="lm")
        return im

    # ---- assets ----
    def _make_card(self, p: Player) -> Image.Image:
        cw, ch = 186, 214
        role = p.role
        bench = p.name not in self.rc.xi_names
        if bench:
            c0, c1 = (214, 220, 216), (190, 199, 194)
        elif role == "C":
            c0, c1 = (247, 255, 214), (223, 255, 130)
        elif role == "V":
            c0, c1 = (244, 254, 255), (186, 249, 255)
        else:
            c0, c1 = (255, 255, 255), (233, 240, 235)
        body = rounded(grad(cw, ch, c0, c1), 22)
        d = ImageDraw.Draw(body)
        edge = LIME if role == "C" else AQUA if role == "V" else (255, 255, 255)
        d.rounded_rectangle([0, 0, cw - 1, ch - 1], 22, outline=A(edge, 1), width=3 if role else 1)
        d.rounded_rectangle([cw * 0.15, 0, cw * 0.85, 5], 3, fill=A(PLUM600, 1))
        if p.name in self.rc.subbed_off:
            tag = "DIDN'T PLAY"
        elif p.name in self.rc.subbed_on:
            tag = f"AUTO-SUB · {p.position}"
        else:
            tag = "BENCH" if bench else p.position
        d.text((cw / 2, 28), tag, font=font("black", 15), fill=A(GREY_TXT if bench else PLUM600, 1), anchor="mm")
        d.text((cw / 2, 58), p.name, font=fit(p.name, "black", 30, cw - 20), fill=A(INK950, 1), anchor="mm")
        d.text((cw / 2, 87), p.club_short, font=fit(p.club_short, "b", 19, cw - 20), fill=A(GREY_TXT, 1), anchor="mm")
        opp = p.opponent_codes
        ft = fit(opp, "b", 19, cw - 60)
        tw = self._chip_width(24, p.venue, opp, ft)
        self._venue_chip(d, cw / 2 - tw / 2, 113, 24, p.venue, opp, ft, A(FIX_TXT, 1))
        d.text((cw / 2, 143), f"forecast {p.xp:.1f}", font=font("sb", 17), fill=A(FIX_TXT, 1), anchor="mm")
        pts = self.rc.pts(p)
        pill = self.pts_pill(pts, 26)
        body.alpha_composite(pill, (int(cw / 2 - pill.width / 2), ch - pill.height - 12))
        dot = BEAT if pts >= p.xp else PINK
        d.ellipse([cw - 26, ch - 34, cw - 14, ch - 22], fill=A(dot, 1))
        canvas = Image.new("RGBA", (cw + 40, ch + 40), (0, 0, 0, 0))
        canvas.alpha_composite(body, (16, 24))
        if role:
            b = self.role_badge(role, 42)
            canvas.alpha_composite(b, (cw + 40 - b.width, 0))
        out = shadowed(canvas, blur=12, off=8, op=95)
        return with_alpha(out, 0.82) if bench else out

    def _make_big_captain(self) -> Image.Image:
        c = self.rc.squad.captain
        w, h = 880, 640
        body = rounded(grad(w, h, (247, 255, 214), (223, 255, 130)), 44)
        d = ImageDraw.Draw(body)
        d.rounded_rectangle([w * 0.2, 0, w * 0.8, 9], 5, fill=A(PLUM600, 1))
        d.text((w / 2, 70), f"{POSITION_WORDS[c.position]}  ·  CAPTAIN", font=font("black", 26),
               fill=A(PLUM600, 1), anchor="mm")
        disp(d, (w / 2, 200), c.name.upper(), 190, A(INK950, 1), max_width=w - 100)
        d.text((w / 2, 320), f"{c.club_short}  ·  {c.fixture}", font=font("xb", 38), fill=A(GREY_TXT, 1),
               anchor="mm")
        d.text((w / 2, 385), f"Forecast {c.xp:.1f} xP  ·  scored {self.rc.pts(c)}", font=font("b", 32),
               fill=A(FIX_TXT, 1), anchor="mm")
        d.rounded_rectangle([w / 2 - 260, 440, w / 2 + 260, 600], 34, fill=A(PLUM900, 1))
        return body

    def _make_vice_row(self) -> Image.Image:
        v = self.rc.squad.vice
        vc = rounded(grad(860, 170, (244, 254, 255), (186, 249, 255)), 34)
        vd = ImageDraw.Draw(vc)
        vd.rounded_rectangle([40, 45, 120, 125], 22, fill=A(AQUA, 1), outline=A(PITCH_D, 1), width=4)
        vd.text((80, 85), "V", font=font("black", 44), fill=A(INK950, 1), anchor="mm")
        pill = self.pts_pill(self.rc.pts(v), 40)
        name_w = 860 - 150 - pill.width - 60
        disp(vd, (150, 62), v.name.upper(), 64, A(INK950, 1), anchor="lm", max_width=name_w)
        sub = f"{v.fixture}  ·  forecast {v.xp:.1f} xP"
        vd.text((150, 122), sub, font=fit(sub, "b", 28, name_w), fill=A(GREY_TXT, 1), anchor="lm")
        vc.alpha_composite(pill, (860 - pill.width - 36, 85 - pill.height // 2))
        return shadowed(vc, 18, 10, 110)

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
        disp(d, (W / 2, 1180 + 50 * (1 - p2)), f"THE {self.MODE}", 130, A(LIME, p2))
        lw = 560 * eo(prog(t, 0.9, 0.6))
        d.rectangle([W / 2 - lw / 2, 1282, W / 2 + lw / 2, 1290], fill=A(PINK, 1))
        d.text((W / 2, 1360), "AI forecast  vs  what actually happened", font=font("sb", 36), fill=A(PAPER, p3),
               anchor="mm")
        pill = ("Forecast locked at the deadline — no hindsight" if self.rc.locked_pre_deadline else
                "Retro forecast — made after the deadline, not a live pick")
        pf = fit(pill, "b", 28, 900)
        pw = pf.getlength(pill) + 64
        d.rounded_rectangle([W / 2 - pw / 2, 1420, W / 2 + pw / 2, 1482], 31, fill=A(PLUM800, p3),
                            outline=A(PLUM600, p3), width=2)
        d.text((W / 2, 1451), pill, font=pf, fill=A(MUTED, p3), anchor="mm")

    def s_score(self, base: Image.Image, t: float) -> None:
        rc = self.rc
        d = ImageDraw.Draw(base)
        p = eo(prog(t, 0.0, 0.45))
        disp(d, (W / 2, 330), "THE SCORE", 96, A(PAPER, p))
        sub = f"Best-xP starting XI ({rc.formation})  ·  captain doubled"
        if rc.subs:
            sub += f"  ·  {len(rc.subs)} auto-sub" + ("s" if len(rc.subs) > 1 else "")
        d.text((W / 2, 410), sub, font=fit(sub, "sb", 30, 980), fill=A(MUTED, p), anchor="mm")
        q = eo(prog(t, 0.3, 1.3))
        pc = eback(prog(t, 0.2, 0.6))
        glow = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse([150, 100, 650, 500], fill=(200, 255, 50, 50))
        paste(base, glow.filter(ImageFilter.GaussianBlur(70)), W / 2, 680, cl(pc))
        disp(d, (W / 2, 660), str(round(rc.total * q)), 360, A(LIME, cl(pc * 1.5)))
        d.text((W / 2, 870), "POINTS", font=font("black", 44), fill=A(PAPER, cl(pc * 1.5)), anchor="mm")
        rows = [("OpenFPL Scout XI", rc.total, LIME, f"{rc.total}")]
        if rc.info.average is not None:
            rows.append((f"GW{self.gw} average manager", rc.info.average, MUTED, f"{rc.info.average}"))
        rows.append(("Locked forecast" if rc.locked_pre_deadline else "Retro forecast", rc.xp_total, AQUA, f"{rc.xp_total:.1f} xP"))
        vmax = max(r[1] for r in rows) * 1.05
        for i, (label, v, col, txt) in enumerate(rows):
            qa = eo(prog(t, 1.4 + i * 0.2, 0.6))
            y = 990 + i * 120
            d.text((90, y), label, font=font("xb", 32), fill=A(PAPER, qa), anchor="lm")
            disp(d, (W - 90, y), txt, 54, A(col, qa), anchor="rm")
            bw = W - 180
            d.rounded_rectangle([90, y + 34, 90 + bw, y + 52], 9, fill=A(PLUM800, qa))
            d.rounded_rectangle([90, y + 34, 90 + max(18, bw * v / vmax * qa), y + 52], 9, fill=A(col, qa))
        if rc.info.average is not None:
            pn = eo(prog(t, 2.4, 0.5))
            d.text((W / 2, 1400), f"{rc.total - rc.info.average:+d} on the GW{self.gw} average",
                   font=font("black", 46), fill=A(PINK, pn), anchor="mm")

    def s_captain(self, base: Image.Image, t: float) -> None:
        rc = self.rc
        c = rc.squad.captain
        d = ImageDraw.Draw(base)
        p = eo(prog(t, 0.0, 0.45))
        disp(d, (W / 2, 330), "THE ARMBAND", 84, A(PAPER, p))
        pc = eback(prog(t, 0.2, 0.6))
        cx, cy = W / 2, 760
        card = self.big_captain.copy()
        cd = ImageDraw.Draw(card)
        mult = 2 if rc.armband_player is c else 1
        cd.text((400, 520), f"{round(rc.pts(c) * mult * eo(prog(t, 0.6, 1.0)))}", font=font("disp", 120),
                fill=A(PAPER, 1), anchor="mm")
        cd.text((548, 545), "PTS", font=font("black", 40), fill=A(LIME, 1), anchor="mm")
        if mult == 2:
            cd.text((670, 470), "×2", font=font("black", 28), fill=A(MUTED, 1), anchor="rm")
        paste(base, shadowed(card, 24, 14, 120), cx, cy, cl(pc * 1.5), scale=0.85 + 0.15 * pc)
        paste(base, self.role_badge("C", 110), cx + 400, cy - 300, cl(prog(t, 0.6, 0.3)),
              scale=0.5 + 0.5 * eback(prog(t, 0.6, 0.5)))
        pv = eo(prog(t, 1.4, 0.5))
        paste(base, self.vice_row, W / 2, 1285 + 40 * (1 - pv), pv)
        pn = eo(prog(t, 2.1, 0.5))
        best = rc.hindsight_captain()
        if rc.armband_player is not c:
            line = f"{c.name} didn't play — {rc.armband_player.name} took the armband"
        elif best.name != c.name:
            line = f"Hindsight armband: {best.name} ({rc.pts(best)} pts)"
        else:
            line = "The armband was the best pick in the 15"
        d.text((W / 2, 1450), line, font=fit(line, "xb", 38, 960), fill=A(PINK, pn), anchor="mm")

    def s_pitch(self, base: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(base)
        p = eo(prog(t, 0.0, 0.5))
        disp(d, (W / 2, 300), "THE FULL 15", 96, A(PAPER, p))
        sub = f"Big number = points  ·  {self.rc.beat_count()}/15 beat their forecast  ·  grey = bench"
        d.text((W / 2, 378), sub, font=fit(sub, "sb", 28, 980), fill=A(MUTED, p), anchor="mm")
        pp = eo(prog(t, 0.1, 0.5))
        paste(base, self.pitch, W / 2, 1000 + 40 * (1 - pp), pp)
        # No row labels: the taller cards fill the gaps and every card shows its position.
        rows = ["FWD", "MID", "DEF", "GK"]
        ys = [600, 870, 1140, 1410]
        order = [3, 2, 1, 0]
        for ri, pos in enumerate(rows):
            start = 0.45 + order.index(ri) * 0.6
            players = sorted(self.sq.by_position(pos), key=lambda pl: (pl.name not in self.rc.xi_names, -pl.xp))
            n = len(players)
            gap = 198 if n == 5 else 236
            for i, pl in enumerate(players):
                q = prog(t, start + i * 0.08, 0.5)
                if q <= 0:
                    continue
                paste(base, self.cards[pl.name], W / 2 + (i - (n - 1) / 2) * gap, ys[ri] + 14 + 26 * (1 - eo(q)),
                      cl(q * 1.6), scale=0.55 + 0.45 * eback(q))

    def s_top(self, base: Image.Image, t: float) -> None:
        rc = self.rc
        d = ImageDraw.Draw(base)
        p = eo(prog(t, 0.0, 0.45))
        disp(d, (W / 2, 320), "TOP 5 HAULS", 104, A(PAPER, p))
        top = rc.hauls(5)
        scale_max = max(rc.pts(top[0]), 8) * 1.08
        for i, pl in enumerate(top):
            q = eo(prog(t, 0.3 + i * 0.18, 0.7))
            al = cl(q * 2)
            y = 480 + i * 148
            pts = rc.pts(pl)
            disp(d, (80, y + 6), f"{i + 1}", 92, A(PINK, al), anchor="lm")
            d.text((160, y - 22), pl.name, font=fit(pl.name, "black", 42, 640), fill=A(PAPER, al), anchor="lm")
            sub = f"{pl.club_short}  ·  {pl.fixture}  ·  forecast {pl.xp:.1f} xP"
            d.text((160, y + 22), sub, font=fit(sub, "sb", 24, 660), fill=A(MUTED, al), anchor="lm")
            bw = 740
            d.rounded_rectangle([160, y + 50, 160 + bw, y + 66], 8, fill=A(PLUM800, al))
            col = LIME if pl.role == "C" else AQUA if pl.role == "V" else PAPER
            d.rounded_rectangle([160, y + 50, 160 + max(16, bw * (pts / scale_max) * q), y + 66], 8, fill=A(col, al))
            mx = 160 + bw * min(1.0, pl.xp / scale_max)
            d.rectangle([mx - 3, y + 44, mx + 3, y + 72], fill=A(AQUA, al))
            disp(d, (W - 70, y + 4), f"{round(pts * q)}", 78, A(col, al), anchor="rm")
        pl2 = eo(prog(t, 2.0, 0.4))
        d.rectangle([W / 2 - 150, 1178, W / 2 - 144, 1202], fill=A(AQUA, pl2))
        d.text((W / 2 - 132, 1190), "= locked forecast (xP)" if rc.locked_pre_deadline else "= retro forecast (xP)", font=font("sb", 24), fill=A(MUTED, pl2), anchor="lm")
        ps = eo(prog(t, 1.6, 0.5))
        sy = 1240
        d.text((W / 2, sy), "WHERE IT MISSED", font=font("black", 28), fill=A(PINK, ps), anchor="mm")
        for j, pl in enumerate(rc.misses(2)):
            y = sy + 40 + j * 118
            pts = rc.pts(pl)
            d.rounded_rectangle([70, y, W - 70, y + 100], 26, fill=A(PLUM800, ps), outline=A(PLUM600, ps), width=2)
            disp(d, (110, y + 50), f"{pts} PT" if pts == 1 else f"{pts} PTS", 54, A(PINK, ps), anchor="lm")
            line1 = f"{pl.name} · {pl.club_short} · {pl.fixture}"
            line2 = f"Forecast {pl.xp:.1f} xP · {pts - pl.xp:+.1f} vs forecast"
            d.text((350, y + 32), line1, font=fit(line1, "black", 27, 620), fill=A(PAPER, ps), anchor="lm")
            d.text((350, y + 70), line2, font=fit(line2, "sb", 24, 620), fill=A(MUTED, ps), anchor="lm")

    def s_outro(self, base: Image.Image, t: float) -> None:
        d = ImageDraw.Draw(base)
        p0 = eback(prog(t, 0.0, 0.6))
        paste(base, self.lion(300), W / 2, 380, cl(p0 * 1.4), scale=0.6 + 0.4 * p0)
        p1, p2, p3 = eo(prog(t, 0.2, 0.5)), eo(prog(t, 0.5, 0.5)), eo(prog(t, 1.0, 0.5))
        disp(d, (W / 2, 610), f"GW{self.rc.cta_gameweek} SQUAD", 110, A(PAPER, p1))
        disp(d, (W / 2, 725), "IS LIVE.", 110, A(LIME, p1))
        tag = ("Same model. Locked at the deadline. Scored in public." if self.rc.locked_pre_deadline else
               "Live forecasts lock at the deadline and get scored in public.")
        d.text((W / 2, 830), tag, font=fit(tag, "sb", 30, 960),
               fill=A(MUTED, p1), anchor="mm")
        for i, (value, label) in enumerate(REPORT_STATS):
            cx = W / 2 + (i - 1) * 312
            q = eo(prog(t, 0.5 + i * 0.12, 0.5))
            d.rounded_rectangle([cx - 146, 890, cx + 146, 1120], 28, fill=A(PLUM800, q), outline=A(PLUM600, q), width=2)
            disp(d, (cx, 965), value, 84, A(LIME, q))
            d.multiline_text((cx, 1052), label, font=font("sb", 23), fill=A(MUTED, q), anchor="mm", align="center",
                             spacing=6)
        d.text((W / 2, 1170), "Validated on a full held-out Premier League season", font=font("sb", 27),
               fill=A(PAPER, p2), anchor="mm")
        pulse = 1.0 if self.still_mode else 1 + 0.025 * math.sin(t * 6)
        bw, bh = 840 * pulse, 136 * pulse
        d.rounded_rectangle([W / 2 - bw / 2, 1290 - bh / 2, W / 2 + bw / 2, 1290 + bh / 2], 68, fill=A(LIME, p3))
        d.text((W / 2, 1290), SITE_URL, font=font("black", 62), fill=A(INK950, p3), anchor="mm")
        d.text((W / 2, 1410), "Free xP for every player  ·  Rate your team /100", font=font("sb", 29),
               fill=A(PAPER, p3), anchor="mm")
        d.text((W / 2, 1462), "Open source on GitHub  ·  @elcaiseri", font=font("m", 27), fill=A(MUTED, p3),
               anchor="mm")


def build_recap(recap: Recap, out_dir: Path, video: bool = True) -> Dict[str, List[Path]]:
    """Write the recap reel, stills, zip and caption."""
    stem = f"openfpl_gw{recap.squad.gameweek:02d}_recap"
    return export(RecapReel(recap), out_dir, stem, recap_caption(recap), video=video)
