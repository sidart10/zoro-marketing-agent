# Frame review — the delivery gate for generated video

A generated clip is not done when the file lands; it is done when its **frames** have been looked at
and nothing in them gives the game away. Models fail *between* the frames a casual glance samples:
a hand grows a finger for 300 ms, a scarf passes through an arm, a pattern on a garment thins out
and comes back, a background arch slides half a metre. Three thumbnails cannot catch that — a
review that only looks at the first / middle / last frame is not a review. Run this on every clip
before you stitch or return it.

## Step A — extract the frames

```
python3 <skill-dir>/scripts/frame_review.py CLIP [CLIP ...] --out <scratch>/frame_review --fps 2
```

Defaults: 2 frames per second into a 4×4 contact sheet per 8 s. For a clip under 6 s, or any clip
that already failed once, use `--fps 4`. When a region matters most — hands, a face, a logo, a
pattern — add `--crop x:y:w:h` (fractions of the frame) so that region is also written out cropped
and upscaled. The script prints each sheet's path and, when the ffmpeg build can't burn labels, a
row-major timestamp legend for its tiles. The manifest `review.json` has every frame path.

## Step B — look at every frame

Open each contact sheet — with `image_analysis` (prompt it with the checklist below and ask for
findings **per tile with its timestamp**), or by viewing it directly when you can see images — and
`video_analysis` on the clip itself for motion-level checks (speed, camera, physics). Look at all the
tiles, in order, and specifically compare **adjacent** tiles for anything that should be continuous
but isn't. When a sheet is unlabelled, read tiles row-major and map them with the legend.

A reviewer that answers "looks good" without quoting timestamps hasn't done the job. Findings are
`t=Xs: <what is wrong>`.

## Step C — the checklist

Mark each item **pass / soft / hard**. A **hard** fail means the clip is rejected and regenerated; a
**soft** fail is a note the user hears about and may be accepted for a draft.

### Anatomy
- Hands: five fingers, plausible joints, no fused or extra digits; a hand doesn't detach, float, or
  change size. *(hard)*
- Limbs and body: no extra limb, no limb that vanishes, no impossible bend, head and torso stay
  attached and proportionate. *(hard)*
- Face: the same person across the whole clip (and across all shots of a multi-shot set) — same
  features, eye spacing, hair and parting; eyes/teeth not smeared; no morphing during a turn.
  *(hard)*

### Subject and product consistency
- The garment or product keeps its shape, colour, length, pattern density and details throughout
  — a print or embroidery that thins, vanishes, reappears, or changes scale between frames is a
  fail; a sleeve, collar, hem or logo that changes design is a fail. *(hard)*
- Accessories and props persist: a scarf, bag, cup, or dupatta doesn't teleport to a new
  position, vanish, or duplicate between adjacent frames. *(hard)*
- Text and logos stay legible and unchanged across frames. *(hard when text is the point)*

### Physics
- Fabric behaves: it drapes, swings, and settles; it does not pass through a body or another piece
  of fabric, go semi-transparent to show what's behind it, or balloon far beyond what a breeze
  would do. *(hard)*
- Motion is physically paced — no sudden teleport of a body part or prop, no action that happens
  faster than a real person could move, no "slow-mo" unless asked. *(hard on teleport, soft on pace)*
- Contact and weight: feet meet the ground, hands grip what they hold, shadows follow the subject.
  *(soft)*

### Scene and camera
- Background is stable: architecture, plants and furniture don't warp, slide, duplicate or change
  design between frames. *(hard on warp / change, soft on mild drift)*
- The camera does what was asked and nothing else — a "locked" shot doesn't drift or zoom; a
  single named move completes; no unprompted reframe, spin, or whip. *(soft unless the brief
  hinges on the move)*
- Light and colour hold across the clip and match the set's other shots. *(soft)*

### Direction
- Only the directed action happens: no unprompted twirl, hand flourish, wave, or head snap;
  nothing added that reads "AI posing." *(soft, hard when it dominates the clip)*
- The action fits the duration — it doesn't stall and loop, or cram in extra beats. *(soft)*

## Step D — decide and act

- **Any hard fail → reject the clip.** Regenerate it — fix the prompt first (name the clean state
  you want: "the dupatta stays draped over her left shoulder for the whole clip", "the camera is
  locked, no drift"), shorten the clip, or switch model — then review again. Never stitch or return
  a clip with a hard fail. Two rejections in a row on the same shot → tell the user what keeps
  failing and which model / shot change you recommend, rather than burning a third attempt blind.
- **Soft fails only** → deliver, and list them with timestamps so the user decides.
- **Clean** → say so, and say what you checked (frames reviewed, fps, what you looked for). "I
  checked it" without that is not a review.

## Reporting

For each clip, one line of verdict plus the timestamped findings, e.g.

```
shot_2 (veo-3.1, 8 s, 16 frames @2fps): REJECT
  t=3.5–4.5s: dupatta fills the frame and the kurta shows through it (fabric phasing) — hard
  t=4.5s: right hand floats detached from the sleeve — hard
  t=0.0 vs 2.5s: embroidery density roughly doubles — hard
```

Then the comparison when several models were tried: which shots passed on which model, and the pick.
