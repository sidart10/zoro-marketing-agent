# Format recipe — product scene restage (e-commerce / lifestyle)

Take a real product photo (often a rough phone shot) and re-photograph the *same* product in a
styled scene. The product's credibility is the whole job — for a handmade or artisanal product, one
invented stitch or warped logo kills the image. Proven on crochet bags, 2026-09-01: four scenes,
all passed zoom QA, one even preserved a loose thread from the source photo.

## The recipe

1. **Extract a production spec first.** Run `image_analysis` on the real photo asking for
   recreation-level precision: exact shade name, material/texture, construction counts (stitch
   rows, panel proportions), every hardware piece (shape, finish, position), and the handmade
   quirks/flaws worth preserving. This spec is the fidelity contract.
2. **Route.** `gpt-image-2` for scene-swap edits (Scene/Subject/Details/Use-case/Constraints
   shape); `nano-banana-pro` 2K for the most texture-faithful editorial takes — on soft/fibrous
   materials it held texture best. Generate a small mixed batch and pick by eye.
3. **Prompt = scene + verbatim spec + preserve clause.** Pass the real photo as
   `reference_images`. Describe the new scene and lighting, then pin the spec from step 1 nearly
   verbatim, ending with an explicit "identical <counts/color/hardware>, nothing redesigned".
   Repeat the full spec word-for-word in every request of the batch.
4. **Zoom QA every output against the real photo** — crop and magnify the failure points:
   hardware (melted metal, wrong geometry), texture (the material reading as a different material
   — e.g. jersey yarn drifting toward raffia), construction counts, color. Any drift →
   regenerate; never ship, never "fix" by editing the product region.
5. **Free-inventing the product with no reference, or cleaning up its handmade quirks, is
   forbidden** — quirks are proof of fidelity (and of the hand that made it).

## Facts worth money

- `gpt-image-2` default quality is **medium** (3:4 ≈ 768×1024, ~$0.05) — fine for IG, soft for a
  site hero. Re-roll at `quality: high` for a crisp master (~$0.15–0.40 by size) instead of
  AI-upscaling a medium output (upscalers hallucinate texture on handmade surfaces).
- `nano-banana-pro` 2K edit ≈ $0.15 flat and returns ~1792×2400.
- The unused takes that passed QA are not waste — they are matched-world b-roll starts for the
  reel (see generating-videos `references/people-and-fashion.md`, "One visual world per reel").
