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
| STARTED | filled by the workflow - self-heals rows stuck in processing |
| COVERR_KEY | optional - free Coverr API key for extra cinematic clips |
| UNSPLASH_KEY | optional - free Unsplash API key for extra HD photos |
| HISTORICAL | optional override: `true`/`false`. Blank = auto-detected from the topic |
| HF_TOKEN | optional - free Hugging Face token for a 2nd AI image option |

## v23 — multi-source footage (Pexels, Pixabay, Coverr, Wikimedia, Unsplash)

Every scene now pulls candidates from FIVE free sources at once and keeps the
best-scoring, highest-resolution match - this is more reliable than a strict
try-A-then-B waterfall because one bad/irrelevant hit from the first source
can no longer "lock in" a wrong clip:

| Source | Type | Key needed | When it helps |
|---|---|---|---|
| Pexels | video | free (already set) | cinematic, moody, real-life HD/4K footage - gets a relevance bonus for non-cartoon, non-dialogue styles |
| Pixabay | video + photo | free (already set) | broad general-purpose stock, used as the baseline |
| Coverr | video | optional `COVERR_KEY` | exclusive modern cinematic b-roll (free tier: 50 req/hr) - skipped silently with no key |
| Wikimedia Commons | photo | none, always on | historical/documentary topics (wars, old leaders, pre-1950 events) - gets a big score boost whenever the topic is auto-detected as historical, or `HISTORICAL=true` is set on the row |
| Unsplash | photo | optional `UNSPLASH_KEY` | extra high-res dramatic photos for the Ken-Burns fallback - skipped silently with no key |

**Historical detection**: the row's PROMPT/TITLE is scanned for words like
`war`, `history`, `empire`, `revolution`, `independence`, `king`, `emperor`,
`1947`, `1971`, `ancient`, `medieval`, etc. When matched, Wikimedia Commons
photos are strongly preferred over generic stock for that video. Set the
`HISTORICAL` column to `true` or `false` to force it either way.

## No-AI-key fallback (cartoon/animation styles)

The AI image chain is now also free with ZERO keys:

1. **Gemini** ("Nano Banana") - best quality + true character consistency via
   reference images. Needs `GEMINI_API_KEY`.
2. **Hugging Face** (FLUX.1-schnell / SDXL) - good quality, needs a free
   `HF_TOKEN` (set it on the Video settings row or as a repo secret `HF_API_TOKEN`).
3. **Pollinations.ai** - the guaranteed fallback. 100% free, no key, no account,
   no payment ever. Used automatically whenever Gemini/HF are unavailable or fail.
   Character "sameness" across scenes comes from a fixed seed per character
   description instead of image-conditioning.

Non-cartoon styles (documentary/presenter) that find no stock video for a
scene now also fall back to a real photo (Wikimedia/Unsplash/Pixabay/Pexels)
animated with Ken Burns motion, instead of a flat gradient - this is what
makes historical documentaries (WW1, WW2, old leaders) look good without any
AI generation at all.

## Repo secrets needed

- `GEMINI_API_KEY` — optional, best AI cartoon image quality (also used by main.py).
- `HF_API_TOKEN` — optional, 2nd-best free AI image quality.
- `GITHUB_TOKEN` — automatic, nothing to do.

Sheet-level optional keys (set per-row or leave blank): `COVERR_KEY`, `UNSPLASH_KEY`, `HF_TOKEN`.

All visuals stay copyright-free: AI-generated images (Gemini/HuggingFace/Pollinations),
Pixabay/Pexels/Coverr stock, Wikimedia Commons public-domain archive, Unsplash photos,
royalty-free music. No logos, no real faces (except public-domain archive), no copyrighted clips.
