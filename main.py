#!/usr/bin/env python3
"""
Full automated video pipeline - runs 100% on GitHub Actions, no n8n.

Flow: Google Sheet (queued topics) -> Gemini script -> Pixabay clips
      -> plan.json -> render.py (Edge TTS + ffmpeg + music + captions
      + presenter avatars + thumbnail) -> GitHub Release -> Sheet update.

One run makes MULTIPLE videos (up to PIPELINE_MAX_VIDEOS or the time
budget), so a long queue drains fast. Repo is public => unlimited minutes.

Required environment (GitHub Secrets):
  GEMINI_API_KEY    - Google AI Studio key (AIza...)
  PIXABAY_API_KEY   - pixabay.com/api/docs key
  GOOGLE_SA_JSON    - full Google service-account JSON (sheet editor)
  SHEET_ID          - optional, falls back to built-in default
  GH_TOKEN          - provided by Actions (no secret needed)
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests

SELFTEST = os.environ.get("PIPELINE_SELFTEST") == "1"
SHEET_ID = os.environ.get("SHEET_ID", "1HwesVw1hGORWq09wWUC_1e3oKgFES85E9vCzr8kklCY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
REPO = "rafivai11233/video-renderer"

MAX_VIDEOS = int(os.environ.get("PIPELINE_MAX_VIDEOS", "5"))       # per run
BUDGET_MIN = float(os.environ.get("PIPELINE_BUDGET_MIN", "50"))    # per run
MAX_CONSECUTIVE_FAILURES = 2

# Sheet columns: A topic | B language | C voice | D status | E job_id
#   F video_url | G thumb_url | H release_url | I error | J style | K avatar
COL = {"topic": 1, "language": 2, "voice": 3, "status": 4, "job_id": 5,
       "video_url": 6, "thumb_url": 7, "release_url": 8, "error": 9,
       "style": 10, "avatar": 11}


# --------------------------------------------------------------- small utils
def slugify(text, maxlen=40):
    s = text.lower()
    s = re.sub(r"[^a-z0-9\u0980-\u09FF]+", "-", s).strip("-")
    return s[:maxlen]


def extract_json(text):
    """Pull a JSON object out of an LLM reply (handles ``` fences)."""
    text = re.sub(r"```(json)?", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in Gemini reply:\n{text[:300]}")
    return json.loads(m.group(0))


def rowval(row, name):
    i = COL[name] - 1
    return row[i].strip() if len(row) > i and row[i] else ""


# -------------------------------------------------------------------- sheet
def open_sheet():
    import gspread  # noqa: late import keeps selftest light
    from google.oauth2.service_account import Credentials
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet("Topics")


def pick_topic(sh):
    """Return (row_number, row) of the first queued row, else (None, None)."""
    for i, row in enumerate(sh.get_all_values()[1:], start=2):
        if not row or not row[0].strip():
            continue
        status = rowval(row, "status").lower()
        if status in ("", "queued"):
            return i, row
    return None, None


def cell(sh, row, name, value):
    sh.update_cell(row, COL[name], value)


# ------------------------------------------------------------------- gemini
def gemini_script(topic, language, style):
    presenter = style in ("presenter", "podcast")
    if language == "bn":
        lang_rule = "Write the title, thumbnail text and ALL narration in Bengali (bangla script)."
        voice_note = "The tone is a warm, direct Bangla speech."
    else:
        lang_rule = "Write everything in English."
        voice_note = "The tone is a warm, direct speech."
    if presenter:
        style_rule = ("This is a TALKING-HEAD presenter video (facecam/podcast style). "
                      "Write it as a passionate first-person motivational speech, "
                      "as if the speaker is talking directly to the camera.")
    else:
        style_rule = ("This is a documentary-style voiceover video. "
                      "Write it as an engaging documentary narration.")

    prompt = f"""Create a ~12-14 minute YouTube video script about: "{topic}"
{lang_rule}
{style_rule}
{voice_note}
Return ONLY valid JSON, exactly this shape:
{{
  "title": "SEO friendly title under 90 chars",
  "thumbnail_text": "3-6 punchy words",
  "music_mood": "one of: calm, dark, energetic",
  "scenes": [
    {{
      "narration": "170-200 words",
      "clip_keywords": ["2-4 word stock footage search phrase", "another phrase"]
    }}
  ]
}}
Rules:
- Exactly 12 scenes, 170-200 narration words each (total ~2,100-2,400 words).
- Scene 1 opens with a strong hook.
- clip_keywords: concrete visual things, good for stock video search. 2-3 per scene."""

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        params={"key": GEMINI_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.85, "maxOutputTokens": 16384},
        },
        timeout=180,
    )
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return extract_json(text)


# ------------------------------------------------------------------ pixabay
def pixabay_clips(keywords, want=3):
    """Return up to `want` stock media URLs for one scene: videos first, images as fallback."""
    urls = []
    if not PIXABAY_KEY:
        return urls
    for kw in keywords[:3]:
        kw = kw.strip()
        if not kw:
            continue
        try:
            r = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": PIXABAY_KEY, "q": kw, "per_page": 5, "safe_search": "true"},
                timeout=30)
            r.raise_for_status()
            for hit in r.json().get("hits", []):
                v = hit.get("videos", {})
                url = (v.get("large") or v.get("medium") or v.get("tiny") or {}).get("url")
                if url and url not in urls:
                    urls.append(url)
                    break
        except Exception as e:  # noqa: BLE001 - a bad keyword must never kill the run
            print(f"pixabay video search failed for '{kw}': {e}", flush=True)
        if len(urls) >= want:
            return urls[:want]
    # fallback: images
    for kw in keywords[:2]:
        try:
            r = requests.get(
                "https://pixabay.com/api/",
                params={"key": PIXABAY_KEY, "q": kw, "per_page": 5,
                        "image_type": "photo", "safe_search": "true"},
                timeout=30)
            r.raise_for_status()
            for hit in r.json().get("hits", []):
                url = hit.get("largeImageURL")
                if url and url not in urls:
                    urls.append(url)
                    break
        except Exception as e:  # noqa: BLE001
            print(f"pixabay image search failed for '{kw}': {e}", flush=True)
        if len(urls) >= want:
            break
    return urls[:want]


# ------------------------------------------------------------------ release
def publish_release(job_id):
    env = dict(os.environ)
    subprocess.run(
        ["gh", "release", "create", job_id, "out/final.mp4", "out/thumbnail.jpg",
         "--title", job_id, "--notes", "Automated render"],
        check=True, env=env)
    # keep only the newest 20 releases
    subprocess.run(
        "gh release list --limit 100 --json tagName --jq '.[20:][].tagName'"
        " | xargs -r -n1 gh release delete --cleanup-tag -y",
        shell=True, check=False, env=env)


def release_urls(job_id):
    r = requests.get(
        f"https://api.github.com/repos/{REPO}/releases/tags/{job_id}",
        headers={"Authorization": f"Bearer {os.environ.get('GH_TOKEN', '')}"},
        timeout=30)
    r.raise_for_status()
    assets = {a["name"]: a["browser_download_url"] for a in r.json().get("assets", [])}
    return assets.get("final.mp4", ""), assets.get("thumbnail.jpg", "")


# --------------------------------------------------------------------- plan
def build_plan(job_id, row, script, scenes):
    topic = rowval(row, "topic")
    language = (rowval(row, "language") or "en")[:2].lower()
    voice = rowval(row, "voice")
    style = (rowval(row, "style") or "documentary").lower()
    avatar = rowval(row, "avatar")
    presenter = style in ("presenter", "podcast")

    if not voice:
        if presenter:
            voice = "bn-BD-NabanitaNeural" if language == "bn" else "en-US-AvaMultilingualNeural"
        else:
            voice = "bn-BD-NabanitaNeural" if language == "bn" else "en-US-AndrewMultilingualNeural"
    if presenter and not avatar:
        v = voice.lower()
        avatar = "boy-1" if ("andrew" in v or "christopher" in v or "pradeep" in v) else "girl-1"

    plan = {
        "job_id": job_id,
        "title": script.get("title") or topic,
        "thumbnail_text": script.get("thumbnail_text") or (script.get("title") or topic),
        "language": language,
        "voice": voice,
        "rate": "-4%",
        "resolution": "720",
        "format": "16:9",
        "style": style,
        "avatar": avatar if presenter else "",
        "avatar_mode": "center" if style == "podcast" else "corner",
        "music_mood": script.get("music_mood") or "calm",
        "music_volume": 0.3,
        "captions": True,
        "resume_url": "",
        "scenes": scenes,
    }
    return plan


# ------------------------------------------------------------------- engine
def make_video(sh):
    """Render ONE queued topic. Returns 'done', 'failed' or 'empty'."""
    row_i, row = pick_topic(sh)
    if row_i is None:
        return "empty"

    topic = rowval(row, "topic")
    job_id = f"{slugify(topic)}-{int(time.time())}"
    print(f"\n=== next topic (row {row_i}): {topic}\njob_id: {job_id}", flush=True)
    cell(sh, row_i, "status", "rendering")
    cell(sh, row_i, "job_id", job_id)
    cell(sh, row_i, "error", "")

    try:
        style = (rowval(row, "style") or "documentary").lower()
        language = (rowval(row, "language") or "en")[:2].lower()

        print("Gemini: writing script...", flush=True)
        script = gemini_script(topic, language, style)
        print(f"Gemini: {len(script.get('scenes', []))} scenes, mood={script.get('music_mood')}", flush=True)

        scenes = []
        for idx, s in enumerate(script["scenes"]):
            clips = []
            if style == "documentary":
                clips = pixabay_clips(s.get("clip_keywords", []))
            scenes.append({"narration": s["narration"], "clips": clips})
            print(f"scene {idx + 1}: {len(s['narration'].split())} words, {len(clips)} clips", flush=True)

        plan = build_plan(job_id, row, script, scenes)
        Path("plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        print(f"plan.json written (style={plan['style']}, voice={plan['voice']}, avatar={plan['avatar']})", flush=True)

        print("render.py: TTS + ffmpeg render starting...", flush=True)
        subprocess.run([sys.executable, "render.py"], check=True)

        print("publishing release...", flush=True)
        publish_release(job_id)
        video_url, thumb_url = release_urls(job_id)
        release_url = f"https://github.com/{REPO}/releases/tag/{job_id}"

        cell(sh, row_i, "status", "done")
        cell(sh, row_i, "video_url", video_url)
        cell(sh, row_i, "thumb_url", thumb_url)
        cell(sh, row_i, "release_url", release_url)
        print(f"DONE: {job_id}\nvideo: {video_url}", flush=True)
        return "done"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        cell(sh, row_i, "status", "failed")
        cell(sh, row_i, "error", str(e)[:200])
        return "failed"


def run_pipeline():
    sh = open_sheet()
    made, failed_streak = 0, 0
    start = time.time()
    while made < MAX_VIDEOS and (time.time() - start) < BUDGET_MIN * 60:
        result = make_video(sh)
        if result == "empty":
            print("Queue is empty - nothing more to do.", flush=True)
            break
        if result == "done":
            made += 1
            failed_streak = 0
        else:
            failed_streak += 1
            if failed_streak >= MAX_CONSECUTIVE_FAILURES:
                print(f"{failed_streak} failures in a row - stopping "
                      "(check the sheet's error column / Actions log).", flush=True)
                return 1
    print(f"run finished: {made} video(s) made in {(time.time() - start) / 60:.1f} min", flush=True)
    return 0 if made or failed_streak == 0 else 1


# ------------------------------------------------------------------ selftest
def run_selftest():
    """Local test: fake script (no Gemini/Pixabay/Sheet/Release), full render."""
    script = {
        "title": "Selftest Presenter Video",
        "thumbnail_text": "SELFTEST OK",
        "music_mood": "energetic",
        "scenes": [
            {"narration": "This is a local self test of the full pipeline. The avatar, the voice, the music and the captions all render together in one pass. If you can hear this and see me talking, everything works.",
             "clip_keywords": []},
            {"narration": "This second scene proves that crossfades, music ducking and subtitle timing all still work when the pipeline is driven by main point python instead of n8n.",
             "clip_keywords": []},
        ],
    }
    row = ["Selftest", "en", "en-US-AvaMultilingualNeural", "", "", "", "", "", "", "podcast", "boy-1"]
    plan = build_plan("selftest-local", row, script, script["scenes"])
    Path("plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    print(f"selftest plan: style={plan['style']} voice={plan['voice']} avatar={plan['avatar']} scenes={len(plan['scenes'])}", flush=True)
    subprocess.run([sys.executable, "render.py"], check=True)
    out = Path("out/final.mp4")
    print(f"SELFTEST OK -> {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    sys.exit(run_selftest() if SELFTEST else run_pipeline())
