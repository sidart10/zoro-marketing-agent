# Field notes — people, fashion, and real-photo sources (learned 2026-08-19, updated 2026-09-01)

Hard-won routing for clips that show a person wearing a product. Read when the brief is on-model
fashion, a brand's real photos exist, or the user says an earlier result "looks AI".

## One visual world per reel (2026-09-01, rejected work)

A reel's shots must share one look, one grade, one level of production polish — "shot on the same
day by the same photographer". A cut that mixed polished AI-restaged scenes with Ken Burns moves on
the raw (graded) source photo was rejected as "mediocre to bad": the world-clash reads cheap and
buries the point of the restaged scenes. If the hero shot is a restaged scene, every shot is a
restaged scene — the unused alternates from the still batch are free b-roll starts. Raw/authentic
footage (maker's hands, process video) is its own reel, never filler inside an editorial one.

## Product still-life cinemagraphs (proven recipe, 2026-09-01)

For product reels with no people: bring a **restaged product still** to life with locked-camera i2v
— Kling 3.0 Pro, 5 s, audio off (~$0.56/clip), start frame = a 9:16 crop of the approved still.
Prompt shape: name the product and state it "stays completely motionless and unchanged for the
entire shot", then give the *environment* the motion (steam curling from a cup, a light band
sliding, leaf shadows trembling, dust motes, a moving glint on hardware) plus "locked tripod
framing with an almost imperceptible slow push-in". Environment moves, product doesn't — passed
frame review three-for-three. Kling recomposes the start frame slightly wider and warms the grade
(see frame-review.md), so frame the crop with margin.

## What reads as AI, and what doesn't

- **AI still → AI video compounds.** Animating a generated still of a person (plastic skin, floaty
  fabric, dead eyes) was rejected outright. Use the brand's **real photos** as the source whenever
  they exist.
- **Image-to-video from a real photo with minimal motion = "the photo, breathing."** Technically
  clean, but the user called it unimpressive. Reserve i2v for b-roll (fabric, product) — not for a
  person the viewer is meant to watch.
- **Real photo as a *reference* → new scene with real motion** is the recipe that passed: she walks
  toward camera / turns at sunset / steps through a doorway in a *new* place, and the likeness,
  garment, and accessories held. Tracking shots and one clear action per clip.
- **Fabric macro b-roll (hand on placket, sleeve ripple) is the most believable footage of all** —
  cloth is forgiving. Open or punctuate a reel with it.
- Slow an ending shot 10–20% (`setpts`) and add a short hold: reads cinematic, covers VO tail.

## Model facts for real people (verified)

| Model | Real person's face in the source | Note |
| --- | --- | --- |
| `veo-3.1` | **Refused** — fal returns `content_policy_violation` on the prompt/image | Don't spend a retry; switch model |
| `kling-3.0-pro` | Accepts a real photo as **start frame** (i2v) | **No reference-only mode** — `reference_images` need a `start_frame_image`; also tends to return square (1440×1440) output for some modes |
| `seedance-2.0` | Accepts real photos as `reference_images` (r2v) | The working recipe: 1 full-body + 1 closer real shot as Image 1/Image 2, `Subject 1@Image 1`, 5–6 s, 9:16, 1080p |

## Cutting to a voiceover

If a VO is going under the reel, **lock the VO first, then cut picture to it** — don't lay VO over
an existing cut. Find the sentence boundaries with
`ffmpeg -i vo.mp3 -af "atempo=<speed>,silencedetect=noise=-32dB:d=0.25" -f null -` and trim each
clip (`-ss`/`-t`) so the action lands on its word (the hand going into the pocket on "pockets").
A 1.03× `atempo` on the VO is inaudible and buys ~0.5 s.

## Stitching gotchas

- Never stretch a clip to the target frame — **crop** to the target ratio, then scale, then
  `setsar=1`. One square clip scaled to 9:16 without `setsar=1` gave the whole reel DAR 1:1 (played
  squashed) because `video_stitch` inherited the first clip's SAR. Probe outputs with
  `ffprobe -show_entries stream=width,height,sample_aspect_ratio,display_aspect_ratio`.
- Homebrew ffmpeg 8 ships **without `subtitles`/`drawtext`** (no libass/freetype). Burn text by
  rendering transparent PNGs (Pillow via `uv run --with pillow`) and `overlay=...:enable='between(t,a,b)'`.
  Futura lacks `₹` and `→` — use Helvetica Neue (has `₹`) or a middot.
- Normalise every input to the same fps (24), 48 kHz stereo AAC, SAR 1 before stitching.
- `job_status` now surfaces a terminal structured error (with a hint) when fal rejects a completed
  job's result — a handle that stays `pending` means the job is genuinely still rendering; keep
  rejoining it, never resubmit.
