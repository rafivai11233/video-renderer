# RAFI Master v5.0 - Free AI Video Pipeline (5-minute videos)

Automated YouTube video factory: Google Sheet prompt -> n8n -> script (Gemini, 2 parts) -> stock clips + photos (Pexels, Pixabay, Wikimedia) -> AI images (Pollinations/Gemini) -> edge-tts voice -> GitHub Actions render (ffmpeg, 1080p) -> Drive + YouTube + Telegram.

## 1. GitHub setup (one time)
- Repo: `rafivai11233/video-renderer`, branch `v22` (workflow uses this).
- **Settings -> Secrets and variables -> Actions -> New repository secret:**
  - `GEMINI_API_KEY` - free key from https://aistudio.google.com (for consistent AI scene images)
  - `HF_API_TOKEN` - optional, https://huggingface.co/settings/tokens (extra free AI image model)
- Workflow file `.github/workflows/render.yml` already reads these. No other setup needed.
- Render machine: Ubuntu + ffmpeg + Python (auto-installed by the workflow).

## 2. n8n setup (one time)
1. Import `n8n/RAFI-master-v5.0.json` (or the downloaded file).
2. Open **Video settings** node and set:
   - `pexels_key` - free key from https://www.pexels.com/api (currently paused for new keys; when you get one, paste it)
   - `pixabay_key` - already baked in, but you can replace with your own from https://pixabay.com/api/docs
3. Credentials to reconnect after import: Google Sheets, Google Drive, Google Gemini, GitHub (HTTP header auth with a token having `repo` scope), YouTube (optional), Telegram (optional).
4. Old workflows: deactivate them so only v5.0 runs.

## 3. Google Sheet columns (same as before)
| Column | Example | Meaning |
|---|---|---|
| PROMPT | see example below | the video brief |
| DURATION | 5 | minutes (1-15) |
| STYLE | 3d | documentary, presenter, podcast, cartoon, animation, 2d, 3d |
| VOICE | (empty) | edge-tts voice override |
| AUDIENCE | kids in Bangladesh | who it is for |
| RESOLUTION | 1080 | 1080 or 720 |
| FORMAT | 16:9 | 16:9 or 9:16 |
| AVATAR | robot-1 | cartoon-boy-1, cartoon-girl-1, boy-1, girl-1, robot-1, grandpa-1, kid-boy-1, bizman-1 |

Header row must contain a cell named **DOC** and one named **STARTED** (status columns).

## 4. Example prompts (paste in PROMPT, set DURATION=5)
- **3D story:** `SCENE 1 ... SCENE 10 | Make a heartwarming 3D animated story about a little robot in Dhaka who learns to fly a kite. | TONE: funny then touching | STYLE: 3d`
- **Documentary (5 min):** `Make a 5-minute documentary about how the Padma Bridge changed the lives of people in southern Bangladesh. | TONE: inspiring, cinematic`
- **Podcast:** `A 5-minute podcast-style monologue about why the night sky in the city has so few stars, ending with hope. | STYLE: podcast`
- **2D explainer:** `Explain in 5 minutes how a rice plant becomes the rice on our plate. Flat 2D style, energetic. | STYLE: 2d`
- **Cartoon:** `A funny Gopal Bhar style cartoon about a clever barber. | STYLE: cartoon`

## 5. Quality guards (the QA nodes - why nothing can silently break)
- **QA 1: fix scenes** - every scene automatically gets keywords, alt_keywords and image_prompt (auto-derived from narration) so clip search never runs on empty.
- **QA 2: fix clip gaps** - every scene always shows the AI-image option to the clip curator, so no scene can end up with a black screen.
- **QA 3: final plan check** - final JSON gate: scenes must have voice, all footage URLs must come from copyright-free whitelisted hosts (pexels, pixabay, wikimedia, pollinations) - anything else is stripped; render is blocked with a clear error if the plan is truly broken.
- **QA 4: check output** - downloaded file must be a real video (min 1 MB) or the row is marked FAILED instead of "done" with a corrupt file.
- Plus in GitHub render: voice check loop (re-synthesizes bad TTS 3 times), AI image fallback for any empty scene, Ken Burns motion on photos, crossfade + burned captions + loudness-normalized audio (-14 LUFS).

## 6. Copyright safety (YouTube-safe)
All footage: Pexels/Pixabay licenses (free commercial use), Wikimedia Commons (public domain/CC), AI-generated images (Pollinations/Gemini - you own the output). Music bed: royalty-free files shipped with the repo. No scraped or copyrighted media anywhere in the chain.

## 7. Audio-video sync
Each scene's length = its voice track length exactly (computed from the generated audio, not estimated), so narration and visuals never drift. Music is sidechain-ducked under the voice.

## 8. Troubleshooting
| Problem | Fix |
|---|---|
| Row stuck "PROCESSING" | GitHub Actions tab - check the failed run's log; column STARTED has the run id |
| "No stock clips found" | paste valid pexels_key / pixabay_key in Video settings |
| Missing AI images (plain backgrounds) | add GEMINI_API_KEY repo secret (consistent cartoon characters need it) |
| Render timeout | keep DURATION <= 15; 5 min renders in ~20-30 min |
| Wrong language voice | set VOICE column, e.g. bn-BD-PradeepNeural |
