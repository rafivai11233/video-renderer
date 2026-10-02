# Video Renderer v22 — AI Cartoon / Podcast Edition

This branch (`v22`) upgrades the renderer with:

1. **AI cartoon scenes** — Gemini image model (`gemini-2.5-flash-image`, "Nano Banana")
   generates every scene picture, with a generated **character reference sheet** so
   the SAME character (same face, hair, clothes) appears in every scene.
   Falls back to copyright-free Pixabay/Pexels stock, then gradients — never dies.
2. **Voice check loop** — every narration/dialogue line is probed after synthesis
   (`ffprobe` duration vs expected speaking time). Broken/silent audio is
   re-synthesized automatically, up to 3 check passes. Bad voice names fall back
   to a safe voice in the same language.
3. **Podcast / talk-show mode** — two AI cartoon characters, two different voices,
   alternating dialogue lines, both characters overlaid on the video.
4. **Best quality default** — 1080p, x264 CRF 18, `tune animation` for cartoon
   styles, Ken Burns motion (zoom in / zoom out / pan) on AI images.
5. **Render-time fix** — job timeout 120 → 330 minutes, scenes rendered in
   parallel, n8n wait raised to 300 minutes, and any failure marks the row
   `retry:` so the next scheduled n8n run automatically rebuilds the video.

## plan.json (what n8n sends)

```
{
  "job_id": "...",              // release tag
  "title": "...", "thumbnail_text": "...",
  "language": "en|bn|hi|ur|ar",
  "style": "documentary|presenter|podcast|cartoon|cartoon-podcast|animation",
  "quality": "best|fast",
  "resolution": "720|1080|2160",
  "format": "16:9|9:16",
  "music_mood": "calm|dramatic|uplifting|mysterious|inspiring",
  "music_volume": 0.3,
  "captions": true,
  "voice": "...",               // single-voice styles
  "voice_a": "...", "voice_b": "...",   // podcast / talk-show styles
  "avatar": "cartoon-boy-1",    // optional, single presenter styles
  "avatars": ["cartoon-boy-1", "cartoon-girl-1"],  // fallback for talk-show
  "ai_images": true,            // generate cartoon scene images
  "image_max": 14,              // max AI images per video
  "characters": [ {"name": "Gopal", "desc": "funny village boy in orange kurta"} ],
  "resume_url": "...",          // n8n webhook for the final callback
  "scenes": [
    // single voice:
    {"narration": "...", "keywords": "...", "alt_keywords": "...", "image_prompt": "..."},
    // or dialogue (podcast / cartoon-podcast):
    {"lines": [{"speaker": "A", "text": "..."}, {"speaker": "B", "text": "..."}],
     "keywords": "...", "alt_keywords": "...", "image_prompt": "..."}
  ]
}
```

Everything except `scenes` is optional — old v21 plans still work.

## Google Sheet columns

| Column | Meaning |
|---|---|
| PROMPT | video idea (required) |
| DURATION | target minutes, default 10 |
| VIDEO / STATUS / THUMBNAIL / TITLE / DESCRIPTION / TAGS | filled by the pipeline |
| DOC | link to the auto-created Google Doc (YouTube pack) |
| STYLE | documentary / presenter / podcast / **cartoon** / **cartoon-podcast** / animation |
| CHARACTERS | e.g. `Gopal: funny village boy in orange kurta; Mimi: clever girl with glasses` |
| IMAGES | max AI images per video (default 14) |
| RESOLUTION | 1080 (default) / 720 / 2160 |
| FORMAT | 16:9 (default) / 9:16 for Shorts |
| VOICE / VOICE_A / VOICE_B | Edge-TTS voice overrides |
| AUDIENCE, RATE, AVATAR | optional extras |

## Repo secrets needed

- `GEMINI_API_KEY` — required for AI cartoon images (also used by main.py).
- `GITHUB_TOKEN` — automatic, nothing to do.

All visuals are copyright-free: AI-generated images, Pixabay/Pexels stock,
royalty-free music. No logos, no real faces, no copyrighted clips.
