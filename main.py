#!/usr/bin/env python3
"""
Full automated video pipeline - runs 100% on GitHub Actions, no n8n.

Flow: Google Sheet (via Apps Script bridge) -> Gemini script -> Pixabay clips
      -> plan.json -> render.py (Edge TTS + ffmpeg + music + captions +
      presenter/cartoon avatars + thumbnail) -> GitHub Release -> Sheet update.

One run makes MULTIPLE videos (up to PIPELINE_MAX_VIDEOS or the time budget).
Repo is public => unlimited Actions minutes.

Required environment (GitHub Secrets):
  GEMINI_API_KEY     - Google AI Studio key (AIza...)
  PIXABAY_API_KEY    - pixabay.com/api/docs key
  APPS_SCRIPT_URL    - deployed Apps Script Web App URL (from the Sheet)
  APPS_SCRIPT_TOKEN  - bridge token (already set)
  GH_TOKEN           - provided by Actions (no secret needed)
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
SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "").strip()
SCRIPT_TOKEN = os.environ.get("APPS_SCRIPT_TOKEN", "").strip()
REPO = "rafivai11233/video-renderer"

MAX_VIDEOS = int(os.environ.get("PIPELINE_MAX_VIDEOS", "5"))       # per run
BUDGET_MIN = float(os.environ.get("PIPELINE_BUDGET_MIN", "50"))     # per run
MAX_CONSECUTIVE_FAILURES = 2

STYLES = ("documentary", "presenter", "podcast", "cartoon", "animation")
VALID_AVATARS = ("girl-1", "boy-1", "cartoon-boy-1", "cartoon-girl-1",
                 "robot-1", "grandpa-1", "kid-boy-1", "bizman-1")


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


# -------------------------------------------------------------------- sheet
def sheet_call(params, timeout=60):
    q = {"token": SCRIPT_TOKEN}
    q.update(params)
    r = requests.get(SCRIPT_URL, params=q, timeout=timeout)
    r.raise_for_status()
    return r.json()


def pick_topic():
    """First queued row -> dict(row, topic, language, voice, style, avatar), or None."""
    d = sheet_call({"action": "read"})
    if not d.get("ok"):
        raise RuntimeError(f"sheet read failed: {d.get('error')}")
    if d.get("empty"):
        return None
    return d


def sheet_update(row, **fields):
    p = {"action": "update", "row": row}
    p.update(fields)
    d = sheet_call(p)
    if not d.get("ok"):
        raise RuntimeError(f"sheet update failed: {d.get('error')}")


# ------------------------------------------------------------------- gemini
def gemini_script(topic, language, style):
    if language == "bn":
        lang_rule = "Write the title, thumbnail text and ALL narration in Bengali (bangla script)."
    else:
        lang_rule = "Write everything in English."

    style_rules = {
        "documentary": ("This is a documentary-style voiceover video. "
                        "Write an engaging documentary narration."),
        "presenter": ("This is a TALKING-HEAD presenter video (facecam style). "
                      "Write a passionate first-person motivational speech, "
                      "talking directly to the camera."),
        "podcast": ("This is a PODCAST-style video. Write it as a warm, "
                    "reflective first-person monologue, like a podcast episode."),
        "cartoon": ("This is a FUNNY CARTOON STORY video (Gopal Bhar / folk-tale "
                    "humor style). Write a hilarious story narration with comic "
                    "timing, funny reactions, and a light lesson at the end. "
                    "Make it feel like a cartoon character telling the story."),
        "animation": ("This is a modern 3D/animated explainer video. Write crisp, "
                      "energetic, visually-driven narration."),
    }
    style_rule = style_rules.get(style, style_rules["documentary"])
    if style == "cartoon":
        lang_rule += " Funny dialogues welcome."

    prompt = f"""Create a ~12-14 minute YouTube video script about: "{topic}"
{lang_rule}
{style_rule}
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
def pixabay_clips(keywords, want=3, style="documentary"):
    """Up to `want` stock media URLs: videos first, images as fallback.
    cartoon/animation styles bias searches toward animated content."""
    urls = []
    if not PIXABAY_KEY:
        return urls
    kws = [k.strip() for k in keywords if k and k.strip()][:3]
    if style == "cartoon":
        kws = [f"cartoon {k}" for k in kws] or ["cartoon animation"]
    elif style == "animation":
        kws = [f"3d animation {k}" for k in kws] or ["3d animation abstract"]

    def search(endpoint, extra, kws):
        for kw in kws[:3]:
            try:
                r = requests.get(
                    f"https://pixabay.com/api/{endpoint}",
                    params={"key": PIXABAY_KEY, "q": kw, "per_page": 5,
                            "safe_search": "true", **extra},
                    timeout=30)
                r.raise_for_status()
                for hit in r.json().get("hits", []):
                    if endpoint == "videos/":
                        v = hit.get("videos", {})
                        url = (v.get("large") or v.get("medium") or v.get("tiny") or {}).get("url")
                    else:
                        url = hit.get("largeImageURL")
                    if url and url not in urls:
                        urls.append(url)
                        break
            except Exception as e:  # noqa: BLE001 - bad keyword never kills the run
                print(f"pixabay failed for '{kw}': {e}", flush=True)
            if len(urls) >= want:
                return
        return

    search("videos/", {}, kws)
    if len(urls) < want:
        search("", {"image_type": "photo"}, kws[:2])
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
def build_plan(job_id, topic, language, voice, style, avatar, script, scenes):
    language = (language or "en")[:2].lower()
    style = (style or "documentary").lower()
    if style not in STYLES:
        style = "documentary"
    presenter = style in ("presenter", "podcast", "cartoon")

    if not voice:
        voice = {"cartoon": {"en": "en-US-JennyNeural", "bn": "bn-BD-NabanitaNeural"},
                 "presenter": {"en": "en-US-AvaMultilingualNeural", "bn": "bn-BD-NabanitaNeural"},
                 "podcast": {"en": "en-US-AvaMultilingualNeural", "bn": "bn-BD-NabanitaNeural"},
                 "animation": {"en": "en-US-AndrewMultilingualNeural", "bn": "bn-BD-NabanitaNeural"},
                 }.get(style, {}).get(language, "en-US-AndrewMultilingualNeural" if language == "en" else "bn-BD-NabanitaNeural")

    if avatar and avatar not in VALID_AVATARS:
        print(f"unknown avatar '{avatar}' - ignoring", flush=True)
        avatar = ""
    if not avatar:
        v = voice.lower()
        male = ("andrew" in v or "christopher" in v or "pradeep" in v)
        if style == "cartoon":
            avatar = "cartoon-boy-1" if male else "cartoon-girl-1"
        elif style == "animation":
            avatar = ""              # animation default: no avatar (clips only)
        elif presenter:
            avatar = "boy-1" if male else "girl-1"
    if not presenter and style != "animation":
        avatar = ""
    presenter_set = ("presenter", "podcast", "cartoon", "animation")
    if avatar and style not in presenter_set:
        avatar = ""

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
        "avatar": avatar,
        "avatar_mode": "center" if style in ("podcast", "cartoon") else "corner",
        "music_mood": script.get("music_mood") or ("energetic" if style in ("cartoon", "animation") else "calm"),
        "music_volume": 0.3,
        "captions": True,
        "resume_url": "",
        "scenes": scenes,
    }
    return plan


# ------------------------------------------------------------------- engine
def make_video():
    """Render ONE queued topic. Returns 'done', 'failed' or 'empty'."""
    t = pick_topic()
    if t is None:
        return "empty"

    topic = t["topic"]
    row = t["row"]
    language = (t.get("language") or "en")[:2].lower()
    style = (t.get("style") or "documentary").lower()
    if style not in STYLES:
        style = "documentary"
    avatar = t.get("avatar") or ""
    voice = t.get("voice") or ""

    job_id = f"{slugify(topic)}-{int(time.time())}"
    print(f"\n=== next topic (row {row}): {topic}\njob_id: {job_id}", flush=True)
    sheet_update(row, status="rendering", job_id=job_id, error="")

    try:
        print("Gemini: writing script...", flush=True)
        script = gemini_script(topic, language, style)
        print(f"Gemini: {len(script.get('scenes', []))} scenes, mood={script.get('music_mood')}", flush=True)

        scenes = []
        for idx, s in enumerate(script["scenes"]):
            clips = []
            if style in ("documentary", "cartoon", "animation"):
                clips = pixabay_clips(s.get("clip_keywords", []), style=style)
            scenes.append({"narration": s["narration"], "clips": clips})
            print(f"scene {idx + 1}: {len(s['narration'].split())} words, {len(clips)} clips", flush=True)

        plan = build_plan(job_id, topic, language, voice, style, avatar, script, scenes)
        Path("plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        print(f"plan.json written (style={plan['style']}, voice={plan['voice']}, avatar={plan['avatar']})", flush=True)

        print("render.py: TTS + ffmpeg render starting...", flush=True)
        subprocess.run([sys.executable, "render.py"], check=True)

        print("publishing release...", flush=True)
        publish_release(job_id)
        video_url, thumb_url = release_urls(job_id)
        release_url = f"https://github.com/{REPO}/releases/tag/{job_id}"

        sheet_update(row, status="done", video_url=video_url,
                     thumb_url=thumb_url, release_url=release_url)
        print(f"DONE: {job_id}\nvideo: {video_url}", flush=True)
        return "done"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        sheet_update(row, status="failed", error=str(e)[:200])
        return "failed"


def run_pipeline():
    made, failed_streak = 0, 0
    start = time.time()
    while made < MAX_VIDEOS and (time.time() - start) < BUDGET_MIN * 60:
        result = make_video()
        if result == "empty":
            print("Queue is empty - nothing more to do.", flush=True)
            break
        if result == "done":
            made += 1
            failed_streak = 0
        else:
            failed_streak += 1
            if failed_streak >= MAX_CONSECUTIVE_FAILURES:
                print(f"{failed_streak} failures in a row - stopping.", flush=True)
                return 1
    print(f"run finished: {made} video(s) in {(time.time() - start) / 60:.1f} min", flush=True)
    return 0 if made or failed_streak == 0 else 1


# ------------------------------------------------------------------ selftest
def run_selftest():
    """Local test: fake script (no Gemini/Pixabay/Sheet/Release), full render."""
    script = {
        "title": "Selftest Cartoon",
        "thumbnail_text": "SELFTEST OK",
        "music_mood": "energetic",
        "scenes": [
            {"narration": "This is a local self test of the cartoon pipeline. The funny cartoon avatar, the comic captions, the voice and the music all render together in one pass.",
             "clip_keywords": []},
            {"narration": "This second scene proves that bright cartoon backgrounds, crossfades and subtitle timing still work when the pipeline runs on GitHub Actions.",
             "clip_keywords": []},
        ],
    }
    plan = build_plan("selftest-local", "Selftest", "en", "", "cartoon", "cartoon-boy-1",
                      script, script["scenes"])
    Path("plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    print(f"selftest plan: style={plan['style']} voice={plan['voice']} avatar={plan['avatar']}", flush=True)
    subprocess.run([sys.executable, "render.py"], check=True)
    out = Path("out/final.mp4")
    print(f"SELFTEST OK -> {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    sys.exit(run_selftest() if SELFTEST else run_pipeline())
