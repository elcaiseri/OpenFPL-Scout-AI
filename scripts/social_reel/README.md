# Social reel

Builds the weekly OpenFPL Scout AI social pack for one gameweek, straight from `/api/scout`:

| File | Use |
| --- | --- |
| `openfpl_gwNN_reel.mp4` | 24.5 s, 1080×1920, silent track — Reels, TikTok, Shorts |
| `openfpl_gwNN_0X_<scene>_4x5.png` ×5 | 1080×1350 carousel, numbered 1/5–5/5 — Instagram, LinkedIn, Facebook |
| `openfpl_gwNN_0X_<scene>_9x16.png` ×5 | 1080×1920 — Stories, photo-mode TikTok, reel cover |
| `openfpl_gwNN_stills.zip` | all ten stills |
| `openfpl_gwNN_caption.txt` | ready-to-paste caption |

Scenes: hook → captain and vice → the full 15 on the pitch → top 5 by xP with club stacks → model and link.

## Run

```bash
# next gameweek (FPL's is_next), fetched live
python -m scripts.social_reel

# a specific gameweek
python -m scripts.social_reel --gameweek 7

# from a saved /api/scout response (when the API isn't reachable from the build machine)
python -m scripts.social_reel --data scout.json --deadline 2026-10-17T10:00:00Z
```

Output lands in `out/social/gwNN/` (git-ignored). `--no-video` skips the MP4.
Needs Pillow, NumPy and `ffmpeg`; with uv: `uv run --with pillow --with numpy python -m scripts.social_reel`.

## Recap (after the gameweek)

`--recap` scores the locked squad against official FPL points and writes a recap pack to `out/social/gwNN_recap/`
(29 s reel, six stills in each format numbered 1/6–6/6, zip, caption):

```bash
# fetches /api/scout?gameweek=6, FPL /event/6/live/ and /bootstrap-static/
python -m scripts.social_reel --recap --gameweek 6

# offline, from saved responses
python -m scripts.social_reel --recap --data scout_gw06.json --live live_gw06.json --bootstrap bootstrap.json
```

Scenes: hook → the score vs the GW average and the locked forecast → captain and vice with actual points and the hindsight armband → the full 15 with points (bench greyed) → top 5 hauls against their forecast, plus the two biggest misses → CTA for the next gameweek.

- The score is the best-xP legal XI (the same rule as `src/team_rating._best_benchmark_lineup`) plus the captain's points again. No auto-subs are applied.
- It refuses (exit 2) to recap a forecast that is not `frozen`, or when any of the 15 has no live points.
- While FPL's `data_checked` is false it prints a warning: points (bonus especially) are provisional.

## Behaviour

- The squad is validated before anything is drawn: 15 players, 2/5/5/3, one captain, one vice, max 3 per club. A bad payload exits with code 2.
- The hook's status line says "Locked at the deadline" for frozen forecasts, otherwise shows the UK deadline and a team-news reminder.
- Doubles show a `×2` chip with both opponents; blanks show `Blank GW`.
- Unknown clubs (e.g. newly promoted) fall back to the first three letters; add them to `CLUB_CODES` in `data.py`.
- The outro's validation figures live in `REPORT_STATS` in `render.py` — update them when the technical report is re-run.

## Brand

Colours, pitch and card styles mirror `static/style.css`. The logo is `assets/openfpl-predictive-lion-frameless-2026.png`.
Fonts are vendored in `assets/social/fonts/` under the SIL Open Font License: Anton (stand-in for the site's Impact display face) and Inter, both merged across Latin, Latin Extended and Greek so accented names render.
