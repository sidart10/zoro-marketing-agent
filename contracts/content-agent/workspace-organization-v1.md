# Workspace organization contract — v1

How the private `workspace/` stays organized, enforced by code. The directory *set* is defined in
`scripts/content_agent/workspace.py` (`WORKSPACE_DIRECTORIES`); this contract defines what goes
*inside* each, the naming grammar, and the lifecycle. Checked by
`scripts/check_workspace_hygiene.py` (structural violations = errors; curation debt = warnings).

## Folder semantics

| Folder | Contents | Rule |
| --- | --- | --- |
| `media/generated/` | **Inbox.** Raw generation output, auto-written. | Files land in a per-project bucket `media/generated/<slug>/`; files loose at the inbox root are a warning — file or archive them at session end. |
| `projects/<slug>/` | **Curated deliverables.** One folder per campaign. | Slug is kebab-case (`riwayat-rida-suit-set`). Must have `README.md` (what shipped + build recipe); should have `FINAL_*` and `product-facts.md`. Suggested subfolders: `shots/`, `stills/`, `ads/`, `chain/`. |
| `archive/rejected/<slug>/` | Takes that failed frame review or were superseded. | Move, never delete. |
| `cache/scratch/` | Session working files (downloads, cut workfiles, frame-review output). | Disposable; never a deliverable's only home. |
| `library/` | Reusable brand assets that outlive one project (logos, fonts, voice picks). | — |
| `channels/` | Per-destination publishing state (captions, variants, post logs). | — |
| `projections/generated/` | Per-channel exports derived from a project's FINAL. | — |
| `evaluations/`, `migrations/` | Evaluation evidence + its migration receipts (`evaluation_migration.py`). | Written by tooling only. |
| `secrets/` | Fail-closed container for secret-shaped files. | Anything NOT secret-shaped in here is an error. Empty is the healthy state. |

## Active project (the routing pointer)

`projects/.active-project.json` — `{"schema_version": 1, "slug": "<kebab-case>"}`
(schema: `active-project-v1.schema.json`). While set, every generation lands in
`media/generated/<slug>/`. Set it at the **start** of a session via the `workspace_project` MCP
tool (`set`/`status`/`list`/`clear`) or `supercmo_skills.paths.set_active_project()`. No pointer →
bare inbox (backward compatible), and the hygiene check nags about loose files.

## Filename grammar

Built only by `supercmo_skills.client.media_stem()`:

```
[<label>_]<capability>_<model>_<hex8>[_<index>].<ext>
label      optional per-request "label" (kebab-case, ≤48 chars) — USE IT: shot1-walkin, endcard
capability image | video | audio
hex8       uniqueness token
```

Curated copies in `projects/<slug>/` may be renamed freely (`FINAL_rida_reel.mp4`,
`shots/shot1_walkin_kling.mp4`) — the grammar governs the inbox, not the curated tree.

## Lifecycle

1. Session start → `workspace_project set <slug>` (creates `projects/<slug>/`, points the inbox).
2. Generate with `label`s → takes accumulate in `media/generated/<slug>/`.
3. Frame review → losers to `archive/rejected/<slug>/`.
4. Deliver → promote picks into `projects/<slug>/` (FINAL, shots/, stills/, README, product-facts).
5. Session end → `python3 scripts/check_workspace_hygiene.py` is clean (or has only known warnings).

## How to verify

- `scripts/check_private_workspace.py` — isolation boundary (privacy).
- `scripts/check_workspace_hygiene.py [--strict] [--json]` — this contract.
- `workspace_project list` — every project's README/FINAL status at a glance.
