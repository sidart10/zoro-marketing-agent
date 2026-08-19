# Field notes — provider tier limits and accent requests (learned 2026-08-19)

## ElevenLabs free tier (BYOK), verified by API errors
- **Library / community voices are blocked over the API** (`402 paid_plan_required`), which on a
  fresh account removes every non-US/UK/AU accent — e.g. all Indian-accent voices. Only `premade`
  stock voices work. Check `category` on the `list_voices` row before promising a voice.
- `wav` output → `403 output_format_not_allowed` (Pro+). Use mp3.
- Music API (`/v1/music`) → `402`. **`/v1/sound-generation` works** and can produce a usable
  20 s instrumental bed from a music-style prompt (sound-FX model; pleasant, not composed).
- Starter plan (~$5/mo) lifts the library-voice and music limits.

## When the brief needs an accent the tier can't give
1. Say so plainly with the error, and offer the upgrade.
2. Audio tags on eleven-v3 (`[warm Indian English accent]`) are hit-or-miss and slowed the read 20%.
3. Only with the user's explicit go-ahead, another engine may be used for that take. In this project
   the user approved **Gemini 2.5 Flash TTS** with the prefix
   *"Speak in a warm, unhurried Indian English accent: "* — voice `Leda` worked; `Kore` and the pro
   model returned `finishReason: OTHER` with no audio. Output is raw PCM s16le 24 kHz mono → wrap
   with ffmpeg. Record the choice in the project notes so the skill rule isn't silently eroded.

## Mixing a reel (what sounded right)
VO at ~0.4 s, music ~0.28, clip ambience ~0.12, 1 s fade-in / 1.7 s fade-out on music, then
`loudnorm=I=-16:TP=-1.5:LRA=11`. Deliver the VO and music separately too.
