# MAY I: finishing pass

Finishing pass for the music video, done 10–12 s at a time. `finish.py` holds the
**house style**, which stays the same for every segment. Each segment gets a small
plan in `plans/` that sets its cuts, beat-synced camera moves and transitions.

```bash
python3 finish.py SOURCE.mp4 output/may_i_segXX_finished.mp4 plans/segXX.json
```

Needs: ffmpeg (libx264), Python 3 with `numpy` and `opencv-python-headless`
(`librosa` is only used to analyze beats). A 12 s segment renders in about 2 minutes on 4 cores.

## What is always preserved
- Every source frame in its place: same frame count, frame rate (29.97), 1920×1080, duration.
- Audio is stream-copied (`-c:a copy`), so it stays bit-identical to the source.
- No added text or objects; the existing title and effects pass through.

## House style (do not change between segments)
| Element | Setting |
|---|---|
| Shot matching | Per-shot exposure nudged toward mid-grey (`(0.5/median)^0.4`, clamped 0.90–1.12) and white balance moved 30% toward neutral. Constant within a shot, so it never flickers. |
| Tone curve | 50/50 blend of linear and an S-curve, soft shoulder above 0.72, blacks at 0.022 and whites at 0.955: rich blacks that keep shadow detail, rolled-off highlights. |
| Colour | Saturation 0.84, dropping to 0.70 in the highlights. Faint teal in the shadows, faint warmth in the highlights. |
| Halation | Highlights above ~0.74 luma, ~20 px blur, red-orange tint, low strength. |
| Grain | Fine (σ0.65), weighted by luminance and strongest in the midtones, mostly mono with a little chroma. Seeded by `global_start_frame + frame`, so it never repeats between segments. |
| Softening | Only on very crisp shots, Gaussian 0.40–0.45 px, so they match the phone/dash footage. No sharpening, ever. |
| Vignette | ~11% at the corners. |
| Camera moves | Zoom about an anchor (keep the face or subject there). Lanczos resampling; 6-sample motion blur whenever the scale changes quickly. Keep the total zoom ≤ ~1.10. |
| Transitions | `flash` (warm film flash, 7 frames), `leak` (orange light leak, 9 frames), `whip` (horizontal directional blur, 6 frames), RGB split hits (±9 px, 4-frame decay). |

## Rules for writing a new segment plan
1. **Cuts:** detect them (histogram difference > 0.25) and list the first frame of each shot, plus the total frame count.
2. **Beats:** run librosa beat tracking with `start_bpm=110`. The song is ~115 BPM (one beat ≈ 15.6 frames).
3. **Melodic hook / atmospheric parts:** slow eased `ramp` pushes (+3–5% per shot) and no punches.
   If a burned-in title spans several cuts, use **one** continuous ramp and the same anchor across them, so the title never jumps.
4. **Aggressive verses:** a `snapback` (+5–7%, 6 frames) on a hard cut, `punch` (+3.5–7%, 2-frame attack, 8–13-frame release) on the biggest hits only, usually 2–4 per 10 s.
5. **Transitions:** at most ~3 per 10 s, only at major musical changes. Leave most cuts clean.
6. **Your effects** (glitches, clones, flares): keep the camera still over them and don't stack transitions on top.
7. **Segment edges:** end each segment at about 1.0× zoom with no transition running, so the next one joins seamlessly.

## Segment log
- **seg01** (0:00–0:12, `first_12.mp4`): street POV → title (push across the B/C/D shots, punch on the 6.5 s drop) → smoke (whip in) → street (snap-back + 3 punches) → crew (light leak in, hardest punch at 11.0 s).
