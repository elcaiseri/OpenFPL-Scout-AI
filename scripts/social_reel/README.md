# Social reel

Builds the weekly OpenFPL Scout AI social pack for one gameweek, straight from `/api/scout`:

| File | Use |
| --- | --- |
| `openfpl_gwNN_preview_reel.mp4` | 29.3 s (24.5 s without differentials), 1080×1920, silent track — Reels, TikTok, Shorts |
| `openfpl_gwNN_preview_0X_<scene>_4x5.png` ×6 | 1080×1350 carousel, numbered 1/6–6/6 (1/5–5/5 without differentials) — Instagram, LinkedIn, Facebook |
| `openfpl_gwNN_preview_0X_<scene>_9x16.png` ×6 | 1080×1920 — Stories, photo-mode TikTok, reel cover |
| `openfpl_gwNN_preview_stills.zip` | all ten stills |
| `openfpl_gwNN_preview_caption.txt` | ready-to-paste caption |

Scenes: hook → captain and vice → the full 15 on the pitch → top 5 by xP with club stacks → the differentials → model and link.

### The differentials card

"THE DIFFERENTIALS": the top 2 by xP in each position (8 players) from the model's full projected pool,
outside the model's 15, owned by under 10% of FPL managers (`selected_by_percent`), available (FPL status `a`)
and with a fixture this gameweek. The thresholds are `DIFF_MAX_OWNED` / `DIFF_PER_POSITION` in `data.py`.
The caption lists them too.

It needs two inputs; when either is missing the card is skipped with a warning and the pack is the usual five scenes:

- the projected pool: `--pool` (a saved `/api/gw/playerpoints?gameweek=N` response), else the scout payload's
  `player_points` (present on frozen gameweeks), else a live fetch when `OPENFPL_API_TOKEN` is set;
- ownership: `--bootstrap` (FPL `/bootstrap-static/`, or a trimmed `{"elements": [{"id", "selected_by_percent", "status"}]}`),
  else a live fetch.

```bash
python -m scripts.social_reel --data scout_gw11.json --pool pool_gw11.json --bootstrap bootstrap.json \
    --deadline 2026-11-21T11:00:00Z
```

`--no-differentials` leaves the card out. When the card is drawn, the preview also writes
`openfpl_gwNN_preview_differentials.json` (the picks, their ownership and a build timestamp) for the recap.

### Differentials in the recap

`--differentials openfpl_gwNN_preview_differentials.json` adds the same card to the recap, scored: each pick's actual
points (lime when it met its xP, pink when it didn't) against its xP, plus the group total vs xP. Put after the hauls,
so the recap becomes 7 scenes (33.8 s). The live response must include those players; any missing → card skipped.

The card says "Picked before the deadline" only when the file came from a preview build timestamped before the
deadline. Anything else (`"source": "retro"`, e.g. a gameweek from before this feature, with ownership rebuilt from
FPL's per-gameweek `selected` counts) is labelled "Retro pick" on the card and in the caption.

## Modes

One generator, two modes. The mode is the hook's headline ("THE PREVIEW" / "THE RECAP") and sits in the brand-bar chip on every scene ("GW7 · PREVIEW").

| Mode | When | Output |
| --- | --- | --- |
| `--mode preview` (default) | before the deadline | `out/social/gwNN_preview/`, files `openfpl_gwNN_preview_*` |
| `--mode recap` | after the gameweek | `out/social/gwNN_recap/`, files `openfpl_gwNN_recap_*` |

## Run (preview)

```bash
# next gameweek (FPL's is_next), fetched live
python -m scripts.social_reel

# a specific gameweek
python -m scripts.social_reel --mode preview --gameweek 7

# from a saved /api/scout response (when the API isn't reachable from the build machine)
python -m scripts.social_reel --data scout.json --deadline 2026-10-17T10:00:00Z
```

Output lands in `out/social/gwNN_preview/` (git-ignored). `--no-video` skips the MP4.
Needs Pillow, NumPy and `ffmpeg`; with uv: `uv run --with pillow --with numpy python -m scripts.social_reel`.

## Recap (after the gameweek)

`--mode recap` (or `--recap`) scores the locked squad against official FPL points and writes a recap pack to `out/social/gwNN_recap/`
(29 s reel, six stills in each format numbered 1/6–6/6, zip, caption):

```bash
# fetches /api/scout?gameweek=6, FPL /event/6/live/ and /bootstrap-static/
python -m scripts.social_reel --mode recap --gameweek 6

# offline, from saved responses
python -m scripts.social_reel --mode recap --data scout_gw06.json --live live_gw06.json --bootstrap bootstrap.json
```

Scenes: hook → the score vs the GW average and the locked forecast → captain and vice with actual points and the hindsight armband → the full 15 with points (bench greyed) → top 5 hauls against their forecast, plus the two biggest misses → (with `--differentials`) the preview's differentials, scored → CTA for the next gameweek.

- The score is the best-xP legal XI (the same rule as `src/team_rating._best_benchmark_lineup`) plus the captain's points again, with FPL auto-subs (a starter with 0 minutes is replaced by the first bench player, in xP order, who played and keeps the formation legal) and the vice taking the armband if the captain did not play.
- Only a forecast frozen with `freeze_method: "deadline"` and captured before the deadline is called "locked". Anything else (e.g. `after-deadline` backfills) is labelled a retro forecast on the hook, score, hauls, outro and caption.
- The CTA points at FPL's `is_next` gameweek, so an old recap still advertises the live squad.
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
