#!/usr/bin/env python3
"""Frame review — extract dense frames + timestamped contact sheets from generated clips.

Generated video fails in the frames, not in the summary: a hand grows a sixth finger for half a
second, embroidery smears between 00:03 and 00:05, a dupatta passes through an arm. Three sampled
frames can't catch that. This script makes every clip inspectable at the frame level so the
reviewer (a model or a person) actually *looks* before delivering.

For each input clip it writes, under <out>/<clip-stem>/:

  frames/f_0000_t0.00s.jpg …    one JPEG per sampled frame (default 2 fps), timestamp in the name
  sheet_01.jpg, sheet_02.jpg … contact sheets of those frames (default 4x4 per sheet), each tile
                               labelled with its timestamp so a finding can be quoted as "t=3.5s"
  crops/ (optional, --crop)    the same frames cropped to a region of interest (x:y:w:h, as
                               fractions 0-1) and upscaled — for hands, faces, a logo, embroidery
  review.json                  a manifest: clip, fps, duration, frame count, every frame path,
                               every sheet path, crop settings — fed to the reviewer

It never judges the frames itself. The judging rubric lives in references/frame-review.md — run
image_analysis / video_analysis over the sheets and crops with that rubric, and fail the clip on
any hard fail.

Usage:
  python3 frame_review.py CLIP [CLIP ...] [--out DIR] [--fps 2] [--cols 4] [--rows 4]
                          [--tile 320] [--crop x:y:w:h] [--crop-scale 2]

Requires ffmpeg + ffprobe on PATH. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys


def die(msg: str, code: int = 1) -> None:
    print(f"frame_review: {msg}", file=sys.stderr)
    sys.exit(code)


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames:format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    st = (data.get("streams") or [{}])[0]
    num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
    try:
        src_fps = float(num) / float(den or 1)
    except ZeroDivisionError:
        src_fps = 0.0
    return {
        "width": int(st.get("width") or 0),
        "height": int(st.get("height") or 0),
        "src_fps": round(src_fps, 3),
        "duration": float((data.get("format") or {}).get("duration") or 0.0),
    }


def extract_frames(clip: str, frames_dir: str, fps: float) -> list[dict]:
    os.makedirs(frames_dir, exist_ok=True)
    # `-vsync vfr` + `showinfo` would give exact pts; simpler: sample at a fixed fps and derive
    # t = index / fps, which is exact for a constant-rate `fps=` filter.
    pattern = os.path.join(frames_dir, "f_%04d.jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", clip,
         "-vf", f"fps={fps}", "-q:v", "2", pattern],
        check=True,
    )
    frames = []
    for name in sorted(os.listdir(frames_dir)):
        if not name.startswith("f_") or not name.endswith(".jpg") or "_t" in name:
            continue
        idx = int(name[2:6]) - 1  # ffmpeg numbers from 1
        t = idx / fps
        new = os.path.join(frames_dir, f"f_{idx:04d}_t{t:.2f}s.jpg")
        os.replace(os.path.join(frames_dir, name), new)
        frames.append({"index": idx, "t": round(t, 2), "path": new})
    return frames


def has_drawtext() -> bool:
    """ffmpeg builds without libfreetype lack `drawtext`; we then skip burned labels and rely on
    row-major tile order + the manifest legend instead."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " drawtext " in out


def _tile_filter(i: int, tile: int, label: str, labels: bool) -> str:
    base = f"[{i}:v]scale={tile}:-2:flags=lanczos,pad=ceil(iw/2)*2:ceil(ih/2)*2"
    if labels:
        base += (f",drawtext=text='{label}':x=8:y=8:fontsize={max(14, tile // 16)}:"
                 f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=4")
    return base + f"[v{i}]"


def make_sheets(frames: list[dict], out_dir: str, cols: int, rows: int, tile: int,
                labels: bool) -> list[str]:
    """Tile frames into contact sheets (row-major), each tile labelled with its timestamp when the
    ffmpeg build supports it."""
    per = cols * rows
    sheets = []
    for s, start in enumerate(range(0, len(frames), per), start=1):
        chunk = frames[start:start + per]
        inputs: list[str] = []
        filt: list[str] = []
        for i, fr in enumerate(chunk):
            inputs += ["-i", fr["path"]]
            filt.append(_tile_filter(i, tile, f"t={fr['t']:.1f}s", labels))
        n = len(chunk)
        # the stack needs a full grid; pad with the last frame duplicated if the chunk is short.
        while n < per and n > 0:
            inputs += ["-i", chunk[-1]["path"]]
            filt.append(_tile_filter(n, tile, "(pad)", labels))
            n += 1
        # layout: tile columns by index; heights vary by aspect so use hstack rows then vstack.
        row_labels = []
        for r in range(rows):
            ins = "".join(f"[v{r * cols + c}]" for c in range(cols))
            filt.append(f"{ins}hstack=inputs={cols}[row{r}]")
            row_labels.append(f"[row{r}]")
        filt.append(f"{''.join(row_labels)}vstack=inputs={rows}[out]")
        sheet = os.path.join(out_dir, f"sheet_{s:02d}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", *inputs,
             "-filter_complex", ";".join(filt), "-map", "[out]", "-q:v", "3", sheet],
            check=True,
        )
        sheets.append(sheet)
    return sheets


def make_crops(frames: list[dict], crops_dir: str, crop: str, scale: float) -> list[str]:
    os.makedirs(crops_dir, exist_ok=True)
    try:
        x, y, w, h = (float(v) for v in crop.split(":"))
    except ValueError:
        die("--crop must be x:y:w:h as fractions 0-1, e.g. 0.3:0.5:0.4:0.3")
    out = []
    for fr in frames:
        dst = os.path.join(crops_dir, os.path.basename(fr["path"]))
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", fr["path"],
             "-vf", f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale=iw*{scale}:ih*{scale}:flags=lanczos",
             "-q:v", "2", dst],
            check=True,
        )
        out.append(dst)
    return out


def review_clip(clip: str, out_root: str, a: argparse.Namespace) -> dict:
    if not os.path.isfile(clip):
        die(f"not a file: {clip}")
    stem = os.path.splitext(os.path.basename(clip))[0]
    out_dir = os.path.join(out_root, stem)
    os.makedirs(out_dir, exist_ok=True)
    info = probe(clip)
    frames = extract_frames(clip, os.path.join(out_dir, "frames"), a.fps)
    sheets = make_sheets(frames, out_dir, a.cols, a.rows, a.tile, a.labels) if frames else []
    # tile legend: sheet -> row-major list of timestamps, so an unlabelled sheet is still quotable
    per = a.cols * a.rows
    legend = {os.path.basename(sh): [fr["t"] for fr in frames[i * per:(i + 1) * per]]
              for i, sh in enumerate(sheets)}
    crops = make_crops(frames, os.path.join(out_dir, "crops"), a.crop, a.crop_scale) if a.crop else []
    manifest = {
        "clip": os.path.abspath(clip),
        **info,
        "sample_fps": a.fps,
        "frame_count": len(frames),
        "frames": frames,
        "sheets": sheets,
        "sheet_labels_burned": a.labels,
        "sheet_legend": legend,
        "grid": {"cols": a.cols, "rows": a.rows, "order": "row-major"},
        "crop": a.crop,
        "crops": crops,
        "rubric": "references/frame-review.md",
    }
    with open(os.path.join(out_dir, "review.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("clips", nargs="+", help="video file(s) to review")
    p.add_argument("--out", default=None, help="output root (default: <clip dir>/frame_review)")
    p.add_argument("--fps", type=float, default=2.0, help="frames sampled per second (default 2)")
    p.add_argument("--cols", type=int, default=4)
    p.add_argument("--rows", type=int, default=4)
    p.add_argument("--tile", type=int, default=320, help="tile width in px on the contact sheet")
    p.add_argument("--crop", default=None, help="x:y:w:h fractions — region of interest to crop+upscale")
    p.add_argument("--crop-scale", type=float, default=2.0)
    a = p.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not found on PATH")
    if a.fps <= 0 or a.cols < 1 or a.rows < 1:
        die("fps, cols and rows must be positive")
    a.labels = has_drawtext()
    if not a.labels:
        print("frame_review: this ffmpeg has no drawtext filter — sheets are unlabelled; "
              "tiles are row-major, see sheet_legend in review.json", file=sys.stderr)

    results = []
    for clip in a.clips:
        out_root = a.out or os.path.join(os.path.dirname(os.path.abspath(clip)), "frame_review")
        m = review_clip(clip, out_root, a)
        results.append(m)
        print(f"{os.path.basename(clip)}: {m['duration']:.2f}s @ {m['width']}x{m['height']} -> "
              f"{m['frame_count']} frames, {len(m['sheets'])} sheet(s)"
              + (f", {len(m['crops'])} crops" if m["crops"] else ""))
        for s in m["sheets"]:
            print(f"  sheet: {s}")
            if not m["sheet_labels_burned"]:
                print(f"    tiles (row-major, s): {m['sheet_legend'][os.path.basename(s)]}")
        print(f"  manifest: {os.path.join(os.path.dirname(m['sheets'][0]) if m['sheets'] else out_root, 'review.json')}")
    # Machine-readable summary on stdout's last line for callers.
    print(json.dumps({"clips": [{"clip": r["clip"], "sheets": r["sheets"], "frames": r["frame_count"]} for r in results]}))


if __name__ == "__main__":
    main()
