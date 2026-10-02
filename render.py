#!/usr/bin/env python3
"""
Free video renderer v22 (runs on GitHub Actions).

plan.json -> edge-tts voice per scene or per dialogue line
          -> VOICE CHECK loop (re-synthesizes broken/silent audio, 3 passes)
          -> AI cartoon scene images via Gemini "Nano Banana" image model
             (character reference sheet keeps characters consistent)
          -> Pixabay/Pexels clips as b-roll fallback (copyright-free only)
          -> Ken Burns motion on images, crossfaded timeline, burned captions
          -> royalty-free music bed + sidechain ducking + -14 LUFS mastering
          -> 1080p default, x264 crf 18 + tune animation for cartoon styles
          -> out/final.mp4 + out/thumbnail.jpg
"""
import asyncio
import base64
import glob
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

FPS = 30
GAP = 0.4                      # silence after each scene's voice (s)
LINE_GAP = 0.3                  # silence between dialogue lines (s)
XF = 0.5                       # crossfade between scenes (s)
XF_FRAMES = int(XF * FPS)
TRANSITIONS = ["fade", "dissolve", "fade", "fadeblack"]

WORK = Path("work")
OUT = Path("out")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
IMAGE_MODELS = [os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"),
                "gemini-2.5-flash-image-preview"]
HF_TOKEN = os.environ.get("HF_API_TOKEN", "").strip()
HF_IMAGE_MODELS = ["black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-xl-base-1.0"]
POLLINATIONS_MODELS = ["flux", "turbo"]   # always free, no key, no account needed

# Presenter avatars (AI-generated, royalty-free) - png files in /avatars
AVATARS = {}
if Path("avatars").exists():
    AVATARS = {p.stem: p for p in Path("avatars").glob("*.png")}

VOICE_FX = (
    "highpass=f=75,"
    "equalizer=f=250:t=q:w=1.2:g=-2,"          # reduce mud
    "equalizer=f=3500:t=q:w=1:g=2.5,"          # presence
    "deesser=i=0.3,"
    "acompressor=threshold=-20dB:ratio=3:attack=8:release=120:makeup=3"
)

FONTS = {
    "bn": "Noto Sans Bengali",
    "hi": "Noto Sans Devanagari",
    "ur": "Noto Sans Naskh Arabic",
    "ar": "Noto Sans Naskh Arabic",
}

# map script moods onto the music files that live in /music
MUSIC_FILE_MOOD = {"calm": "calm", "dramatic": "dark", "uplifting": "energetic",
                   "mysterious": "dark", "inspiring": "calm", "energetic": "energetic",
                   "dark": "dark"}

CARTOON_STYLE_LINE = ("Bright colorful 2D TV cartoon illustration, clean bold outlines, "
                      "flat colors with soft shading, expressive funny faces, detailed "
                      "background, high quality animated TV series screenshot, 16:9")
ANIMATION_STYLE_LINE = ("Modern 3D animated explainer style, soft lighting, glossy "
                        "materials, cinematic depth, high quality render, 16:9")
PODCAST_STYLE_LINE = ("Cozy modern podcast studio illustration, warm lighting, "
                     "microphones, high quality 2D cartoon TV series style, 16:9")


# ----------------------------------------------------------------- helpers
def sh(cmd):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd)[:500], flush=True)
    subprocess.run(cmd, check=True)


def ff(*args):
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args])


def probe(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def words_of(text):
    return len([w for w in str(text or "").split() if w.strip()])


def dims(plan):
    res = str(plan.get("resolution", "720"))
    if res == "2160":
        w, h = 3840, 2160
    elif res == "1080":
        w, h = 1920, 1080
    else:
        w, h = 1280, 720
    if str(plan.get("format", "16:9")).strip() == "9:16":
        w, h = h, w
    return w, h


def best_quality(plan):
    return str(plan.get("quality", "best")).lower() != "fast"


def is_cartoonish(style):
    return style in ("cartoon", "cartoon-podcast", "animation", "podcast")


def scene_text(scene):
    """Full spoken text of a scene (single narration or dialogue lines)."""
    if scene.get("lines"):
        return " ".join(l.get("text", "") for l in scene["lines"])
    return scene.get("narration", "")


# --------------------------------------------------------------------- TTS
def _voice_fallback(voice):
    """A safe backup voice in the same language if the requested one is wrong."""
    v = str(voice).lower()
    if v.startswith("bn"):
        return "bn-BD-NabanitaNeural"
    if v.startswith("hi"):
        return "hi-IN-SwaraNeural"
    if v.startswith("ur"):
        return "ur-PK-AsadNeural"
    if v.startswith("ar"):
        return "ar-SA-HamedNeural"
    return "en-US-AndrewMultilingualNeural"


async def tts_one(text, voice, rate, out, fallback=None):
    """Synthesize one chunk. Retries up to 5x and CHECKS the audio each time
    (file size + real duration vs expected speaking time)."""
    import edge_tts
    expected = max(1.0, words_of(text) / 2.6)     # ~156 wpm
    bad_voice = False
    for attempt in range(5):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(out))
            ok = False
            if Path(out).exists() and Path(out).stat().st_size > 2000:
                try:
                    dur = probe(out)
                    ok = dur >= max(0.8, expected * 0.35)
                    if not ok:
                        print(f"voice check FAILED for {out.name}: "
                              f"{dur:.1f}s audio for ~{expected:.1f}s text - retrying",
                              flush=True)
                except Exception:
                    ok = False
            if ok:
                return
        except Exception as e:  # network / throttling / bad voice
            msg = str(e).lower()
            if "voice" in msg or "no audio" in msg or "404" in msg:
                bad_voice = True
                break
            print("TTS retry", attempt, repr(e), flush=True)
        await asyncio.sleep(2 + attempt * 3)
    if bad_voice and fallback and fallback != voice:
        print(f"WARNING: voice '{voice}' is invalid - falling back to '{fallback}'", flush=True)
        for attempt in range(2):
            try:
                await edge_tts.Communicate(text, fallback, rate=rate).save(str(out))
                if Path(out).exists() and Path(out).stat().st_size > 2000:
                    return
            except Exception as e:  # noqa: BLE001
                print("fallback retry", attempt, repr(e), flush=True)
            await asyncio.sleep(2)
    raise RuntimeError(
        f"TTS failed for {out} - voice '{voice}' "
        f"({'invalid voice name' if bad_voice else 'network error'})")


async def tts_plan(scenes, voice, voice_a, voice_b, rate, dialogue):
    """Synthesize every narration and every dialogue line (3 at a time)."""
    sem = asyncio.Semaphore(3)

    async def one(text, v, out):
        async with sem:
            await tts_one(text, v, rate, out, fallback=_voice_fallback(v))

    tasks = []
    for i, s in enumerate(scenes):
        if dialogue and s.get("lines"):
            for j, ln in enumerate(s["lines"]):
                v = voice_a if str(ln.get("speaker", "A")) == "A" else voice_b
                tasks.append(one(str(ln.get("text", "")), v, WORK / f"t{i:03d}_{j:03d}.mp3"))
        else:
            tasks.append(one(scene_text(s), voice, WORK / f"v{i:03d}.mp3"))
    await asyncio.gather(*tasks)


def voice_check(scenes, dialogue):
    """VOICE CHECK pass: find scenes whose audio is missing/too short/silent.
    Returns list of (scene_index, line_index_or_None) that must be re-synthesized."""
    broken = []
    for i, s in enumerate(scenes):
        expected = max(1.5, words_of(scene_text(s)) / 2.6)
        if dialogue and s.get("lines"):
            for j in range(len(s["lines"])):
                p = WORK / f"t{i:03d}_{j:03d}.mp3"
                try:
                    if not p.exists() or p.stat().st_size < 2000 or probe(p) < max(0.5, expected * 0.1):
                        broken.append((i, j))
                except Exception:
                    broken.append((i, j))
        else:
            p = WORK / f"v{i:03d}.mp3"
            try:
                if not p.exists() or p.stat().st_size < 2000 or probe(p) < max(0.5, expected * 0.35):
                    broken.append((i, None))
            except Exception:
                broken.append((i, None))
    return broken


async def voice_repair(broken, scenes, voice, voice_a, voice_b, rate, dialogue):
    sem = asyncio.Semaphore(2)

    async def one(text, v, out):
        async with sem:
            await tts_one(text, v, rate, out, fallback=_voice_fallback(v))

    tasks = []
    for (i, j) in broken:
        s = scenes[i]
        if j is None or not s.get("lines"):
            tasks.append(one(scene_text(s), voice, WORK / f"v{i:03d}.mp3"))
        else:
            ln = s["lines"][j]
            v = voice_a if str(ln.get("speaker", "A")) == "A" else voice_b
            tasks.append(one(str(ln.get("text", "")), v, WORK / f"t{i:03d}_{j:03d}.mp3"))
    await asyncio.gather(*tasks)


# ------------------------------------------------------------- AI images
def pollinations_image(prompt, out_png, seed=None, tries=3):
    """Free, no-key AI image generation via Pollinations.ai (Flux/Turbo).
    This is the guaranteed fallback - always works, no account, no cost."""
    import urllib.parse
    seed = seed if seed is not None else random.randint(1, 999999)
    for model in POLLINATIONS_MODELS:
        url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:900])
        for attempt in range(tries):
            try:
                r = requests.get(url, params={
                    "width": 1280, "height": 720, "nologo": "true",
                    "seed": seed, "model": model, "enhance": "true"}, timeout=90)
                if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
                    Path(out_png).write_bytes(r.content)
                    return True
                time.sleep(6 + attempt * 6)
            except Exception as e:  # noqa: BLE001
                print(f"pollinations retry {attempt} ({model}): {e}", flush=True)
                time.sleep(6 + attempt * 6)
    print("Pollinations image failed after retries - using motion/photo fallback", flush=True)
    return False


def huggingface_image(prompt, out_png, tries=2):
    """Optional higher-quality free image via Hugging Face Inference API
    (needs a free HF_API_TOKEN). Skipped silently if no token is set."""
    if not HF_TOKEN:
        return False
    for model in HF_IMAGE_MODELS:
        for attempt in range(tries):
            try:
                r = requests.post(
                    f"https://api-inference.huggingface.co/models/{model}",
                    headers={"Authorization": f"Bearer {HF_TOKEN}"},
                    json={"inputs": prompt[:900], "options": {"wait_for_model": True}},
                    timeout=90)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    Path(out_png).write_bytes(r.content)
                    return True
                if r.status_code in (503, 429):
                    time.sleep(15 + attempt * 15)
                    continue
                break   # model unavailable/4xx - try next model
            except Exception as e:  # noqa: BLE001
                print(f"huggingface retry {attempt} ({model}): {e}", flush=True)
                time.sleep(10)
    return False


def ai_image(prompt, ref_pngs, out_png, seed=None):
    """Best-available free AI image: Gemini (consistent chars, needs key) ->
    Hugging Face (needs free token) -> Pollinations (always free, no key)."""
    if GEMINI_KEY and gemini_image(prompt, ref_pngs, out_png):
        return True
    if huggingface_image(prompt, out_png):
        return True
    return pollinations_image(prompt, out_png, seed=seed)


def gemini_image(prompt, ref_pngs, out_png, tries=3):
    """Generate one image with the Gemini image model ('Nano Banana').
    ref_pngs keeps characters consistent across scenes. Returns True/False."""
    if not GEMINI_KEY:
        print("GEMINI_API_KEY missing - AI image generation skipped", flush=True)
        return False
    parts = [{"text": prompt}]
    for ref in ref_pngs:
        try:
            b = Path(ref).read_bytes()
            mime = "image/png"
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(b).decode()}})
        except Exception as e:  # noqa: BLE001
            print("ref image load failed:", e, flush=True)
    last_err = ""
    for model in IMAGE_MODELS:
        for attempt in range(tries):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": GEMINI_KEY},
                    json={"contents": [{"parts": parts}],
                          "generationConfig": {"responseModalities": ["IMAGE"]}},
                    timeout=120)
                if r.status_code == 404:
                    break                       # model name not available - try next
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(15 + attempt * 15)
                    continue
                r.raise_for_status()
                data = r.json()
                img = None
                for part in (data.get("candidates", [{}])[0].get("content", {}).get("parts") or []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        img = inline
                        break
                if not img:
                    raise ValueError("model returned no image (safety filter?)")
                Path(out_png).write_bytes(base64.b64decode(img["data"]))
                return True
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                print(f"image gen retry {attempt} ({model}): {e}", flush=True)
                time.sleep(10 + attempt * 10)
    print(f"AI image failed after retries ({last_err[:200]}) - using fallback visuals", flush=True)
    return False


def build_character_refs(plan):
    """Character portrait refs -> work/char_0.png, work/char_1.png.
    Reference images only help the Gemini path keep faces consistent; the
    Pollinations/HuggingFace paths reuse a fixed seed per character instead."""
    chars = [c for c in plan.get("characters", []) if c.get("desc") or c.get("name")]
    refs = []
    for k, c in enumerate(chars[:2]):
        out = WORK / f"char_{k}.png"
        prompt = (f"Character portrait of {c.get('name', 'character')}: "
                  f"{c.get('desc', '')}. Chest up, centered, facing viewer. "
                  "Bright colorful 2D TV cartoon style, clean bold outlines, flat colors "
                  "with soft shading, expressive face, simple plain background. No text.")
        seed = (abs(hash(c.get("name", "") + c.get("desc", ""))) % 900000) + 1
        if ai_image(prompt, [], out, seed=seed):
            refs.append(out)
        time.sleep(2)
    return refs


def build_scene_images(plan, refs):
    """One AI image per scene (up to plan.image_max), consistent characters.
    Always available even with no API key at all (Pollinations.ai fallback)."""
    scenes = plan["scenes"]
    style = str(plan.get("style", "documentary")).lower()
    style_line = {"cartoon": CARTOON_STYLE_LINE,
                  "cartoon-podcast": CARTOON_STYLE_LINE,
                  "animation": ANIMATION_STYLE_LINE,
                  "podcast": PODCAST_STYLE_LINE}.get(style, CARTOON_STYLE_LINE)
    chars = [c for c in plan.get("characters", []) if c.get("name")]
    char_desc = "; ".join(f"{c.get('name')}: {c.get('desc', '')}" for c in chars[:2])
    char_seed = (abs(hash(char_desc)) % 900000) + 1 if char_desc else None
    img_max = int(plan.get("image_max", 14))
    images = {}
    made = 0
    for i, s in enumerate(scenes):
        ip = str(s.get("image_prompt", "")).strip()
        if not ip or made >= img_max:
            continue
        prompt = (f"{style_line}\n"
                  + (f"Main characters (keep the SAME look as described): {char_desc}\n"
                     if char_desc else "")
                  + f"Scene: {ip}\n"
                  "Consistent character design across scenes. "
                  "No text, no watermark, no logo.")
        out = WORK / f"img{i:03d}.png"
        if ai_image(prompt, refs, out, seed=char_seed):
            images[i] = out
            made += 1
            if made % 5 == 0:
                print(f"AI images: {made}/{min(img_max, len(scenes))}", flush=True)
        time.sleep(2 if not GEMINI_KEY else 8)   # Gemini free tier needs gentler pacing
    print(f"AI scene images: {len(images)} generated "
          f"({'Gemini' if GEMINI_KEY else ('HuggingFace' if HF_TOKEN else 'Pollinations')} path)", flush=True)
    return images


# ------------------------------------------------------------------- clips
def download(url, dest):
    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            return True
        except Exception as e:  # noqa: BLE001
            print("download retry", attempt, url[:80], repr(e), flush=True)
    return False


def fetch_clip(url):
    dest = WORK / "clips" / (hashlib.md5(url.encode()).hexdigest() + ".mp4")
    if not dest.exists() and not download(url, dest):
        return None
    try:
        dur = probe(dest)
    except Exception:
        return None
    return {"path": dest, "dur": dur} if dur >= 1.5 else None


def fetch_photo(url):
    """Download a copyright-free photo for the no-AI-key fallback. Returns path or None."""
    try:
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        if len(r.content) < 20000:
            return None
        dest = WORK / "photos" / (hashlib.md5(url.encode()).hexdigest() + ".jpg")
        dest.write_bytes(r.content)
        return dest
    except Exception as e:
        print(f"photo failed {url[:60]}: {e}", flush=True)
        return None


def make_scene(clips, nframes, out, W, H, blur=False, c0="0x0f2027", c1="0x2c5364",
               quality=True):
    """Cut jump-cuts from stock clips and join them into one exact-length scene."""
    if not clips:
        ff("-f", "lavfi", "-i",
           f"gradients=s={W}x{H}:d={nframes / FPS + 1:.2f}:r={FPS}:speed=0.02:c0={c0}:c1={c1}",
           "-frames:v", nframes, "-c:v", "libx264", "-preset", "superfast",
           "-crf", "17" if quality else "18", "-pix_fmt", "yuv420p", out)
        return
    segs, left, k = [], nframes, 0
    while left > 0:
        want = int(random.uniform(4.0, 6.5) * FPS)
        if left - want < int(1.5 * FPS):
            want = left
        clip = clips[k % len(clips)]
        n = max(1, min(left, want, int((clip["dur"] - 0.1) * FPS)))
        slack = clip["dur"] - n / FPS - 0.05
        start = random.uniform(0, slack) if slack > 0 else 0.0
        segs.append((clip["path"], start, n))
        left -= n
        k += 1
    grade = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
             + ("boxblur=6:2," if blur else "")
             + f"setsar=1,fps={FPS},eq=contrast=1.04:saturation=1.08,format=yuv420p,"
             "setpts=PTS-STARTPTS")
    args, fparts = [], []
    for j, (path, start, n) in enumerate(segs):
        args += ["-ss", f"{start:.3f}", "-t", f"{n / FPS + 0.2:.3f}", "-i", path]
        fparts.append(f"[{j}:v]{grade}[a{j}]")
    labels = "".join(f"[a{j}]" for j in range(len(segs)))
    fparts.append(f"{labels}concat=n={len(segs)}:v=1:a=0,"
                  "tpad=stop_mode=clone:stop_duration=3[v]")
    ff(*args, "-filter_complex", ";".join(fparts), "-map", "[v]",
       "-frames:v", nframes, "-an", "-c:v", "libx264", "-preset", "fast" if quality else "superfast",
       "-crf", "17" if quality else "18", "-pix_fmt", "yuv420p", out)


def image_scene(img, nframes, out, W, H, i, quality=True):
    """Ken Burns motion (alternating zoom in / zoom out / pan) on an AI image."""
    big_w, big_h = int(W * 1.6), int(H * 1.6)
    motion = i % 3
    if motion == 0:      # slow zoom in
        step = 0.10 / max(nframes, 1)
        z = f"min(zoom+{step:.6f},1.10)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif motion == 1:    # slow zoom out
        step = 0.10 / max(nframes, 1)
        z = f"if(eq(on,1),1.10,max(zoom-{step:.6f},1.0))"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    else:                # pan left to right at fixed zoom
        z = "1.08"
        x = f"(iw-iw/zoom)*(on/{max(nframes, 1)})"
        y = "ih/2-(ih/zoom/2)"
    vf = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
          f"crop={big_w}:{big_h},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={nframes}:s={W}x{H}:fps={FPS},"
          "eq=contrast=1.06:saturation=1.12,format=yuv420p")
    ff("-i", img, "-vf", vf, "-frames:v", nframes,
       "-c:v", "libx264", "-preset", "fast" if quality else "superfast",
       "-crf", "17" if quality else "18", "-pix_fmt", "yuv420p", out)


def circle_avatar(src, size, out):
    """Square avatar PNG -> circular avatar PNG with a soft edge ring."""
    ff("-i", src, "-vf",
       f"scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size},"
       "format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
       "a='if(lt(hypot(X-W/2,Y-H/2),W/2-4),255,if(lt(hypot(X-W/2,Y-H/2),W/2-1),140,0))'",
       "-frames:v", "1", out)


def presenter_overlay(scene, avatar_png, x, y, out, quality=True, dur=None):
    """Composite one circular avatar over a rendered scene video."""
    # IMPORTANT: cap the looped image input with -t, otherwise the encode
    # never terminates (the video stream ends but the looped png keeps feeding)
    loop_args = ["-loop", "1"] + (["-t", f"{dur:.3f}"] if dur else []) + ["-i", avatar_png]
    ff("-i", scene, *loop_args,
       "-filter_complex", f"[1:v]format=rgba[av];[0:v][av]overlay={x}:{y}:shortest=1[v]",
       "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "fast" if quality else "superfast",
       "-crf", "18", "-pix_fmt", "yuv420p", out)


def podcast_overlay(scene, avatar_a, avatar_b, out, W, H, quality=True, dur=None):
    """Two circular characters, bottom-left and bottom-right (podcast / talk show)."""
    A = int(W * 0.26)
    y = H - A - int(H * 0.05)
    xA = int(W * 0.05)
    xB = W - A - int(W * 0.05)
    t = ["-t", f"{dur:.3f}"] if dur else []
    ff("-i", scene, "-loop", "1", *t, "-i", avatar_a, "-loop", "1", *t, "-i", avatar_b,
       "-filter_complex",
       f"[1:v]scale={A}:{A},format=rgba[a1];"
       f"[2:v]scale={A}:{A},format=rgba[a2];"
       f"[0:v][a1]overlay={xA}:{y}[p];[p][a2]overlay={xB}:{y}:shortest=1[v]",
       "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "fast" if quality else "superfast",
       "-crf", "18", "-pix_fmt", "yuv420p", out)


# ------------------------------------------------------------------- music
def pick_music(mood):
    exts = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
    files = [p for p in Path("music").rglob("*") if p.suffix.lower() in exts] \
        if Path("music").exists() else []
    if not files:
        return None
    want = MUSIC_FILE_MOOD.get(str(mood or "").lower())
    if want:
        moody = [p for p in files if want in str(p).lower()]
        if moody:
            files = moody
    return random.choice(files)


def build_music(total, mood):
    bed = WORK / "music_bed.wav"
    fade = f"afade=t=in:d=3,afade=t=out:st={max(total - 5, 0):.2f}:d=5"
    track = pick_music(mood)
    if track:
        print("music:", track, flush=True)
        ff("-stream_loop", "-1", "-i", track, "-t", f"{total:.3f}",
           "-af", fade, "-ar", "48000", "-ac", "2", bed)
    else:
        print("music: no files in /music, synthesizing ambient pad", flush=True)
        ins = []
        for f in (110, 164.81, 220, 277.18):
            ins += ["-f", "lavfi", "-i", f"sine=f={f}:r=48000:d={total:.3f}"]
        ff(*ins, "-filter_complex",
           "[0][1][2][3]amix=inputs=4:normalize=0,tremolo=f=0.2:d=0.5,"
           "lowpass=f=1500,aecho=0.8:0.7:600|1100:0.3|0.2,volume=0.6," + fade,
           "-ac", "2", bed)
    return bed


def mix_audio(voice_all, bed, music_vol, out):
    fc = (f"[1:a]volume={music_vol}[m];"
          "[0:a]asplit=2[vk][vm];"
          "[m][vk]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450:makeup=1[duck];"
          "[vm][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mx];"
          "[mx]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,alimiter=limit=0.95[aout]")
    ff("-i", voice_all, "-i", bed, "-filter_complex", fc, "-map", "[aout]",
       "-ar", "48000", "-ac", "2", out)


# ---------------------------------------------------------------- captions
def cs_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def chunk_words(text, maxw=7):
    chunks, cur = [], []
    for w in text.split():
        cur.append(w)
        if len(cur) >= maxw or (len(cur) >= 3 and re.search(r"[.!?,;:\u0964\u061f]$", w)):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        if chunks and len(cur) < 3:
            chunks[-1] += " " + " ".join(cur)
        else:
            chunks.append(" ".join(cur))
    return chunks


def clean_ass(t):
    return t.replace("{", "").replace("}", "").replace("\\", "").replace("\n", " ")


def write_ass(path, events, lang, W, H, style="documentary"):
    """events: list of (chunk_text, t_start, t_end) - one subtitle each."""
    font = FONTS.get(lang, "Noto Sans")
    size = int(H * 0.058) if W >= H else int(W * 0.062)
    if style in ("cartoon", "cartoon-podcast"):
        size = int(size * 1.15)
        style_line = (f"Style: Default,{font},{size},&H0000E8D8,&H000000FF,&H00000000,&H80000000,"
                      f"1,-1,0,0,100,100,0,0,1,5,2,2,"
                      f"{int(W * 0.06)},{int(W * 0.06)},{int(H * 0.07)},1")
    else:
        style_line = (f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
                      f"1,0,0,0,100,100,0,0,1,3,1,2,"
                      f"{int(W * 0.06)},{int(W * 0.06)},{int(H * 0.07)},1")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "", "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        style_line, "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for chunk, t0, t1 in events:
        lines.append(f"Dialogue: 0,{cs_time(t0)},{cs_time(max(t1, t0 + 0.4))},Default,,0,0,0,,{chunk}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def caption_events(scenes, starts, speech_units, lang, names):
    """Build one subtitle event per caption chunk.
    speech_units: per scene, either a single duration (single voice) or a list of
    (text, duration) for dialogue lines."""
    events = []
    for i, s in enumerate(scenes):
        units = speech_units[i]
        base = starts[i]
        if isinstance(units, list):
            t = base
            for j, (text, d) in enumerate(units):
                chunks = chunk_words(clean_ass(text))
                if not chunks:
                    continue
                name = names[j % len(names)] if names else ""
                prefix = f"{name}: " if j == 0 and name else ""
                # first chunk carries the speaker name
                weights = [max(len(c), 1) for c in chunks]
                tot = sum(weights)
                t0 = t + 0.12
                span = max(d - 0.1, 0.5)
                tt = t0
                for ci, (c, wgt) in enumerate(zip(chunks, weights)):
                    dur = span * wgt / tot
                    events.append((prefix + c if ci == 0 else c, tt, tt + dur))
                    tt += dur
                t += d + LINE_GAP
        else:
            chunks = chunk_words(clean_ass(scene_text(s)))
            if not chunks:
                continue
            t0 = base + 0.12
            span = max(units - 0.05, 0.5)
            weights = [max(len(c), 1) for c in chunks]
            tot = sum(weights)
            tt = t0
            for c, wgt in zip(chunks, weights):
                dur = span * wgt / tot
                events.append((c, tt, tt + dur))
                tt += dur
    return events


# ------------------------------------------------------------ final render
def render_final(scene_files, slots, ass, audio, out, total, W, H,
                 quality=True, tune=""):
    n = len(scene_files)
    ins = []
    for f in scene_files:
        ins += ["-i", f]
    ins += ["-i", audio]
    tail = (f"ass={ass},fade=t=in:st=0:d=0.8,"
            f"fade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5,format=yuv420p[vout]")
    parts, cur, cum = [], "[0:v]", 0.0
    for k in range(1, n):
        cum += slots[k - 1] / FPS
        tr = TRANSITIONS[(k - 1) % len(TRANSITIONS)]
        parts.append(f"{cur}[{k}:v]xfade=transition={tr}:duration={XF}:offset={cum:.4f}[x{k}]")
        cur = f"[x{k}]"
    parts.append(f"{cur}{tail}")
    enc = ["-map", "[vout]", "-map", f"{n}:a", "-c:v", "libx264",
           "-preset", "faster" if quality else "veryfast",
           "-crf", "18" if quality else "21",
           "-pix_fmt", "yuv420p",
           "-r", FPS, "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", "-t", f"{total:.3f}"]
    if tune:
        enc = ["-map", "[vout]", "-map", f"{n}:a", "-c:v", "libx264",
               "-preset", "faster" if quality else "veryfast",
               "-crf", "18" if quality else "21",
               "-tune", tune, "-pix_fmt", "yuv420p",
               "-r", FPS, "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", "-t", f"{total:.3f}"]
    try:
        ff(*ins, "-filter_complex", ";".join(parts), *enc, out)
    except subprocess.CalledProcessError:
        print("xfade render failed -> falling back to hard cuts", flush=True)
        lst = WORK / "concat.txt"
        with open(lst, "w") as f:
            for sf, fr in zip(scene_files, slots):
                f.write(f"file '{Path(sf).resolve()}'\noutpoint {fr / FPS:.4f}\n")
        tail_args = enc[4:]
        ff("-f", "concat", "-safe", "0", "-i", lst, "-i", audio,
           "-filter_complex", f"[0:v]{tail}",
           "-map", "[vout]", "-map", "1:a", *tail_args, out)


def make_thumbnail(video, text, lang, out, W, H):
    font = FONTS.get(lang, "Noto Sans")
    words = clean_ass(text or "").upper().split() if lang == "en" else clean_ass(text or "").split()
    half = math.ceil(len(words) / 2)
    txt = " ".join(words[:half]) + ("\\N" + " ".join(words[half:]) if len(words) > 2 else "")
    ass = WORK / "thumb.ass"
    ass.write_text(
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,"
        "Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: T,{font},{int(H * 0.096)},&H0000E6FF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,1,60,60,60,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:10.00,T,,0,0,0,,{txt}\n", encoding="utf-8")
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          "eq=contrast=1.12:saturation=1.3,"
          "drawbox=x=0:y=ih*0.45:w=iw:h=ih*0.55:color=black@0.45:t=fill,"
          f"ass={ass}")
    ff("-ss", "2", "-i", video, "-vf", vf, "-frames:v", "1", "-q:v", "2", out)


# -------------------------------------------------------------------- main
def main():
    global GEMINI_KEY
    plan = json.load(open("plan.json", encoding="utf-8"))
    if not GEMINI_KEY and plan.get("gemini_key"):
        # advanced fallback: key passed inside the plan (WARNING: visible in
        # the public Actions run inputs - prefer the GEMINI_API_KEY secret)
        GEMINI_KEY = str(plan["gemini_key"]).strip()
    random.seed(plan.get("job_id", "x"))
    WORK.mkdir(exist_ok=True)
    (WORK / "clips").mkdir(exist_ok=True)
    (WORK / "photos").mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)

    W, H = dims(plan)
    quality = best_quality(plan)
    scenes = plan["scenes"]
    n = len(scenes)
    lang = str(plan.get("language", "en")).lower()[:2]
    style = str(plan.get("style", "documentary")).lower()
    avatar_mode = str(plan.get("avatar_mode", "corner")).lower()
    dialogue = style in ("podcast", "cartoon-podcast") and any(s.get("lines") for s in scenes)
    bg_blur = style == "podcast" or avatar_mode == "center" or dialogue
    tune = "animation" if style in ("cartoon", "cartoon-podcast", "animation") else ""
    C0, C1 = {"cartoon": ("0xFFB75C", "0xFF7B6B"),
              "cartoon-podcast": ("0xFFB75C", "0xFF7B6B"),
              "animation": ("0x4776E6", "0x8E54E9"),
              "podcast": ("0x2C3E50", "0x4CA1AF")}.get(style, ("0x0f2027", "0x2c5364"))
    voice = plan.get("voice", "en-US-AndrewMultilingualNeural")
    voice_a = plan.get("voice_a") or _voice_fallback(voice if not str(voice).endswith("NabanitaNeural") else voice)
    voice_b = plan.get("voice_b") or _voice_fallback(voice_a)
    chars = [c for c in plan.get("characters", []) if c.get("name")]
    names = [c["name"] for c in chars[:2]] or ["Host", "Guest"]
    if not voice_a or voice_a == voice_b:
        voice_b = _voice_fallback(voice_b)
    print(f"{n} scenes, {W}x{H}, style={style}, dialogue={dialogue}, "
          f"voice={voice}, A={voice_a}, B={voice_b}, quality={'best' if quality else 'fast'}",
          flush=True)

    # 1. VOICE (with a check-repair loop: never let one bad TTS kill the video)
    asyncio.run(tts_plan(scenes, voice, voice_a, voice_b,
                         plan.get("rate", "+0%"), dialogue))
    for qa_pass in range(1, 4):
        broken = voice_check(scenes, dialogue)
        print(f"VOICE CHECK pass {qa_pass}: "
              f"{'all ' + str(n) + ' scenes OK' if not broken else str(len(broken)) + ' broken -> repairing'}",
              flush=True)
        if not broken:
            break
        asyncio.run(voice_repair(broken, scenes, voice, voice_a, voice_b,
                                 plan.get("rate", "+0%"), dialogue))
    still = voice_check(scenes, dialogue)
    if still:
        raise RuntimeError(f"voice check failed for {len(still)} scene(s) after 3 passes")

    # 2. assemble exact-length per-scene voice wavs (+ caption units)
    silence = WORK / "silence.wav"
    ff("-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", f"{LINE_GAP:.3f}", silence)
    slots, speech_units, wavs = [], [], []
    for i in range(n):
        s = scenes[i]
        final_wav = WORK / f"sv{i:03d}.wav"
        if dialogue and s.get("lines"):
            units, line_wavs, cum = [], [], 0.0
            for j, ln in enumerate(s["lines"]):
                mp3 = WORK / f"t{i:03d}_{j:03d}.mp3"
                lw = WORK / f"lw{i:03d}_{j:03d}.wav"
                ff("-i", mp3, "-af", f"{VOICE_FX},aresample=48000", "-ar", "48000",
                   "-ac", "2", lw)
                d = probe(lw)
                units.append((str(ln.get("text", "")), d))
                line_wavs.append(lw)
                cum += d
            lst = WORK / f"lines{i:03d}.txt"
            entries = []
            for k, lw in enumerate(line_wavs):
                entries.append(lw)
                if k < len(line_wavs) - 1:
                    entries.append(silence)
            lst.write_text("".join(f"file '{Path(e).resolve()}'\n" for e in entries))
            raw = WORK / f"raw{i:03d}.wav"
            ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", raw)
            speech = probe(raw)
            units2 = [(t, d) for (t, d) in units]
            fr = math.ceil((speech + GAP) * FPS)
            slot = fr / FPS
            ff("-i", raw, "-af", f"aresample=48000,apad=whole_dur={slot:.4f}",
               "-t", f"{slot:.4f}", "-ar", "48000", "-ac", "2", final_wav)
            slots.append(fr)
            speech_units.append(units2)
            wavs.append(final_wav)
        else:
            mp3 = WORK / f"v{i:03d}.mp3"
            sp = probe(mp3)
            fr = math.ceil((sp + GAP) * FPS)
            slot = fr / FPS
            ff("-i", mp3, "-af", f"{VOICE_FX},aresample=48000,apad=whole_dur={slot:.4f}",
               "-t", f"{slot:.4f}", "-ar", "48000", "-ac", "2", final_wav)
            slots.append(fr)
            speech_units.append(sp)
            wavs.append(final_wav)
    lst = WORK / "voices.txt"
    lst.write_text("".join(f"file '{Path(w).resolve()}'\n" for w in wavs))
    voice_all = WORK / "voice_all.wav"
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", voice_all)
    total = sum(slots) / FPS
    starts = [sum(slots[:i]) / FPS for i in range(n)]
    print(f"total length {total:.1f}s ({total / 60:.1f} min)", flush=True)

    # 3. AI cartoon images (consistent characters) - before clips so they exist early
    images = {}
    if plan.get("ai_images") and is_cartoonish(style):
        refs = build_character_refs(plan)
        images = build_scene_images(plan, refs)
        print(f"AI images ready: {len(images)} for {n} scenes", flush=True)
    img_pool = list(images.values())

    # 4. stock clips (b-roll / fallback)
    urls = sorted({u for s in scenes for u in s.get("clips", [])})
    with ThreadPoolExecutor(6) as ex:
        got = dict(zip(urls, ex.map(fetch_clip, urls)))
    pool = [c for c in got.values() if c]
    print(f"clips downloaded: {len(pool)}/{len(urls)}", flush=True)

    # 4b. scene photos (no-AI-key fallback: Ken Burns over free stock photos)
    photo_urls = sorted({u for s in scenes for u in s.get("photos", []) if u})
    photo_by_scene = {}
    if photo_urls:
        with ThreadPoolExecutor(6) as ex:
            pgot = dict(zip(photo_urls, ex.map(fetch_photo, photo_urls)))
        for i, s in enumerate(scenes):
            for u in s.get("photos", []):
                if pgot.get(u):
                    photo_by_scene[i] = pgot[u]
                    break
        print(f"photos ready: {len(photo_by_scene)}/{n} scenes", flush=True)

    # 5. scene videos (exact length, +crossfade overlap on all but last)
    def build_one(i):
        s = scenes[i]
        clips = [got[u] for u in s.get("clips", []) if got.get(u)]
        frames = slots[i] + (XF_FRAMES if i < n - 1 else 0)
        f = WORK / f"scene{i:03d}.mp4"
        img = images.get(i)
        if img is None and is_cartoonish(style) and photo_by_scene:
            img = photo_by_scene.get(i)                # no AI key: free photo + motion
        if img is None and is_cartoonish(style) and img_pool:
            img = img_pool[i % len(img_pool)]          # reuse with different motion
        if img is not None:
            image_scene(img, frames, f, W, H, i, quality)
        elif clips:
            make_scene(clips, frames, f, W, H, bg_blur, C0, C1, quality)
        elif pool:
            make_scene(random.sample(pool, min(2, len(pool))), frames, f, W, H,
                       bg_blur, C0, C1, quality)
        else:
            make_scene([], frames, f, W, H, bg_blur, C0, C1, quality)
        return f

    with ThreadPoolExecutor(2) as ex:
        scene_files = list(ex.map(build_one, range(n)))

    # 6. character overlays
    overlay_styles = ("presenter", "podcast", "facecam", "cartoon", "cartoon-podcast", "animation")
    if style in overlay_styles:
        if dialogue:
            a_png, b_png = WORK / "char_0.png", WORK / "char_1.png"
            av = plan.get("avatars") or []
            if (not a_png.exists() or not b_png.exists()) and av:
                # fall back to repo avatars
                names_av = [x for x in av if x in AVATARS]
                if len(names_av) >= 2:
                    a_png, b_png = AVATARS[names_av[0]], AVATARS[names_av[1]]
            if Path(a_png).exists() and Path(b_png).exists():
                overlay_files = []
                for i, sf in enumerate(scene_files):
                    po = WORK / f"pscene{i:03d}.mp4"
                    podcast_overlay(sf, a_png, b_png, po, W, H, quality,
                                    dur=slots[i] / FPS + 1)
                    overlay_files.append(po)
                scene_files = overlay_files
                print(f"podcast overlay: {names[0]} + {names[1]}", flush=True)
            else:
                print("podcast avatars missing - skipping overlay", flush=True)
        elif AVATARS:
            av_name = str(plan.get("avatar") or
                          ("cartoon-boy-1" if style in ("cartoon", "cartoon-podcast")
                           else ("girl-1" if "girl-1" in AVATARS else sorted(AVATARS)[0])))
            if av_name in AVATARS:
                A = int(W * (0.40 if avatar_mode == "center" else 0.28))
                circ = WORK / f"avatar_{av_name}_{A}.png"
                circle_avatar(AVATARS[av_name], A, circ)
                # prefer the AI-generated character portrait when available
                char0 = WORK / "char_0.png"
                if char0.exists() and style in ("cartoon", "cartoon-podcast"):
                    circle_avatar(char0, A, circ)
                x, y = ((W - A) // 2, int(H * 0.14)) if avatar_mode == "center" \
                    else (W - A - int(W * 0.03), H - A - int(H * 0.05))
                overlay_files = []
                for i, sf in enumerate(scene_files):
                    po = WORK / f"pscene{i:03d}.mp4"
                    presenter_overlay(sf, circ, x, y, po, quality,
                                      dur=slots[i] / FPS + 1)
                    overlay_files.append(po)
                scene_files = overlay_files
                print(f"presenter overlay: {av_name} mode={avatar_mode} size={A}px", flush=True)
            else:
                print(f"avatar '{av_name}' not found (have: {sorted(AVATARS)}) - skipping", flush=True)

    # 7. audio: music bed + ducking + loudness
    bed = build_music(total, plan.get("music_mood"))
    final_audio = WORK / "final_audio.wav"
    mix_audio(voice_all, bed, float(plan.get("music_volume", 0.3)), final_audio)

    # 8. captions + final timeline
    ass = WORK / "subs.ass"
    if plan.get("captions", True):
        events = caption_events(scenes, starts, speech_units, lang, names if dialogue else [])
        write_ass(ass, events, lang, W, H, style)
    else:
        write_ass(ass, [], lang, W, H, style)
    render_final(scene_files, slots, ass, final_audio, OUT / "final.mp4", total,
                 W, H, quality, tune)

    # 9. thumbnail (frame from the hook scene, so no captions in it)
    make_thumbnail(scene_files[0], plan.get("thumbnail_text") or plan.get("title", ""),
                   lang, OUT / "thumbnail.jpg", W, H)
    print("done", flush=True)


if __name__ == "__main__":
    main()
