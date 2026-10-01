#!/usr/bin/env python3
"""
Free video renderer (runs on GitHub Actions).

plan.json  ->  edge-tts voice  ->  voice cleanup (EQ/de-ess/compress)
           ->  Pixabay clips cut + graded + joined per scene
           ->  crossfaded timeline, burned captions
           ->  royalty-free music bed (repo /music folder, or synthesized pad)
           ->  sidechain ducking + loudness mastering (-14 LUFS)
           ->  out/final.mp4 + out/thumbnail.jpg
"""
import asyncio
import glob
import hashlib
import json
import math
import random
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

FPS = 30
GAP = 0.4                      # silence after each scene's voice (s)
XF = 0.5                       # crossfade between scenes (s)
XF_FRAMES = int(XF * FPS)
TRANSITIONS = ["fade", "dissolve", "fade", "fadeblack"]

WORK = Path("work")
OUT = Path("out")

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
    "ur": "Noto Naskh Arabic",
    "ar": "Noto Naskh Arabic",
}


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


def dims(plan):
    hd = str(plan.get("resolution", "720")) == "1080"
    w, h = (1920, 1080) if hd else (1280, 720)
    if str(plan.get("format", "16:9")).strip() == "9:16":
        w, h = h, w
    return w, h


# --------------------------------------------------------------------- TTS
async def tts_one(text, voice, rate, out):
    import edge_tts
    for attempt in range(5):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(out))
            if Path(out).exists() and Path(out).stat().st_size > 2000:
                return
        except Exception as e:  # network / throttling
            print("TTS retry", attempt, repr(e), flush=True)
        await asyncio.sleep(2 + attempt * 3)
    raise RuntimeError(f"TTS failed: {out}")


async def tts_all(scenes, voice, rate):
    sem = asyncio.Semaphore(3)

    async def one(i, s):
        async with sem:
            await tts_one(s["narration"], voice, rate, WORK / f"v{i:03d}.mp3")

    await asyncio.gather(*(one(i, s) for i, s in enumerate(scenes)))


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
        except Exception as e:
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


def make_scene(i, clips, nframes, out, W, H):
    """Cut jump-cuts from the clips and join them into one exact-length scene."""
    if not clips:
        ff("-f", "lavfi", "-i",
           f"gradients=s={W}x{H}:d={nframes / FPS + 1:.2f}:r={FPS}:speed=0.02:c0=0x0f2027:c1=0x2c5364",
           "-frames:v", nframes, "-c:v", "libx264", "-preset", "superfast",
           "-crf", "18", "-pix_fmt", "yuv420p", out)
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
             f"setsar=1,fps={FPS},eq=contrast=1.04:saturation=1.08,format=yuv420p,"
             "setpts=PTS-STARTPTS")
    args, fparts = [], []
    for j, (path, start, n) in enumerate(segs):
        args += ["-ss", f"{start:.3f}", "-t", f"{n / FPS + 0.2:.3f}", "-i", path]
        fparts.append(f"[{j}:v]{grade}[a{j}]")
    labels = "".join(f"[a{j}]" for j in range(len(segs)))
    fparts.append(f"{labels}concat=n={len(segs)}:v=1:a=0,"
                  "tpad=stop_mode=clone:stop_duration=3[v]")
    ff(*args, "-filter_complex", ";".join(fparts), "-map", "[v]",
       "-frames:v", nframes, "-an", "-c:v", "libx264", "-preset", "superfast",
       "-crf", "18", "-pix_fmt", "yuv420p", out)


# ------------------------------------------------------------------- music
def pick_music(mood):
    exts = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
    files = [p for p in Path("music").rglob("*") if p.suffix.lower() in exts] \
        if Path("music").exists() else []
    if not files:
        return None
    if mood:
        moody = [p for p in files if mood.lower() in [x.lower() for x in p.parts]]
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


def write_ass(path, scenes, starts, speech, lang, W, H):
    font = FONTS.get(lang, "Noto Sans")
    size = int(H * 0.058) if W >= H else int(W * 0.062)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "", "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        f"1,0,0,0,100,100,0,0,1,3,1,2,{int(W * 0.06)},{int(W * 0.06)},{int(H * 0.07)},1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for s, start, sp in zip(scenes, starts, speech):
        chunks = chunk_words(clean_ass(s["narration"]))
        if not chunks:
            continue
        t0, t1 = start + 0.12, start + max(sp - 0.05, 0.5)
        weights = [max(len(c), 1) for c in chunks]
        tot = sum(weights)
        t = t0
        for c, wgt in zip(chunks, weights):
            d = (t1 - t0) * wgt / tot
            lines.append(f"Dialogue: 0,{cs_time(t)},{cs_time(t + d)},Default,,0,0,0,,{c}")
            t += d
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------ final render
def render_final(scene_files, slots, ass, audio, out, total, W, H):
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
    enc = ["-map", "[vout]", "-map", f"{n}:a", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "21", "-r", FPS, "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", "-t", f"{total:.3f}"]
    try:
        ff(*ins, "-filter_complex", ";".join(parts), *enc, out)
    except subprocess.CalledProcessError:
        print("xfade render failed -> falling back to hard cuts", flush=True)
        lst = WORK / "concat.txt"
        with open(lst, "w") as f:
            for sf, fr in zip(scene_files, slots):
                f.write(f"file '{Path(sf).resolve()}'\noutpoint {fr / FPS:.4f}\n")
        ff("-f", "concat", "-safe", "0", "-i", lst, "-i", audio,
           "-filter_complex", f"[0:v]{tail}",
           "-map", "[vout]", "-map", "1:a", *enc[4:], out)


def make_thumbnail(video, text, lang, out):
    font = FONTS.get(lang, "Noto Sans")
    words = clean_ass(text or "").upper().split() if lang == "en" else clean_ass(text or "").split()
    half = math.ceil(len(words) / 2)
    txt = " ".join(words[:half]) + ("\\N" + " ".join(words[half:]) if len(words) > 2 else "")
    ass = WORK / "thumb.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,"
        "Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: T,{font},104,&H0000E6FF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,1,60,60,60,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:10.00,T,,0,0,0,,{txt}\n", encoding="utf-8")
    vf = ("scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
          "eq=contrast=1.12:saturation=1.3,"
          "drawbox=x=0:y=ih*0.45:w=iw:h=ih*0.55:color=black@0.45:t=fill,"
          f"ass={ass}")
    ff("-ss", "2", "-i", video, "-vf", vf, "-frames:v", "1", "-q:v", "2", out)


# -------------------------------------------------------------------- main
def main():
    plan = json.load(open("plan.json", encoding="utf-8"))
    random.seed(plan.get("job_id", "x"))
    WORK.mkdir(exist_ok=True)
    (WORK / "clips").mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)

    W, H = dims(plan)
    scenes = plan["scenes"]
    n = len(scenes)
    lang = str(plan.get("language", "en")).lower()[:2]
    print(f"{n} scenes, {W}x{H}, voice={plan.get('voice')}", flush=True)

    # 1. voice
    asyncio.run(tts_all(scenes, plan.get("voice", "en-US-AndrewMultilingualNeural"),
                        plan.get("rate", "+0%")))
    slots, speech, wavs = [], [], []
    for i in range(n):
        mp3, wav = WORK / f"v{i:03d}.mp3", WORK / f"v{i:03d}.wav"
        sp = probe(mp3)
        fr = math.ceil((sp + GAP) * FPS)
        slot = fr / FPS
        ff("-i", mp3, "-af", f"{VOICE_FX},aresample=48000,apad=whole_dur={slot:.4f}",
           "-t", f"{slot:.4f}", "-ar", "48000", "-ac", "2", wav)
        slots.append(fr)
        speech.append(sp)
        wavs.append(wav)
    lst = WORK / "voices.txt"
    lst.write_text("".join(f"file '{Path(w).resolve()}'\n" for w in wavs))
    voice_all = WORK / "voice_all.wav"
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", voice_all)
    total = sum(slots) / FPS
    starts = [sum(slots[:i]) / FPS for i in range(n)]
    print(f"total length {total:.1f}s", flush=True)

    # 2. clips
    urls = sorted({u for s in scenes for u in s.get("clips", [])})
    with ThreadPoolExecutor(6) as ex:
        got = dict(zip(urls, ex.map(fetch_clip, urls)))
    pool = [c for c in got.values() if c]
    print(f"clips downloaded: {len(pool)}/{len(urls)}", flush=True)

    # 3. scene videos (exact length, +crossfade overlap on all but last)
    scene_files = []
    for i, s in enumerate(scenes):
        clips = [got[u] for u in s.get("clips", []) if got.get(u)]
        if not clips and pool:
            clips = random.sample(pool, min(2, len(pool)))
        frames = slots[i] + (XF_FRAMES if i < n - 1 else 0)
        f = WORK / f"scene{i:03d}.mp4"
        make_scene(i, clips, frames, f, W, H)
        scene_files.append(f)

    # 4. audio: music bed + ducking + loudness
    bed = build_music(total, plan.get("music_mood"))
    final_audio = WORK / "final_audio.wav"
    mix_audio(voice_all, bed, float(plan.get("music_volume", 0.3)), final_audio)

    # 5. captions + final timeline
    ass = WORK / "subs.ass"
    if plan.get("captions", True):
        write_ass(ass, scenes, starts, speech, lang, W, H)
    else:
        write_ass(ass, [], [], [], lang, W, H)
    render_final(scene_files, slots, ass, final_audio, OUT / "final.mp4", total, W, H)

    # 6. thumbnail (frame from the hook scene, so no captions in it)
    make_thumbnail(scene_files[0], plan.get("thumbnail_text") or plan.get("title", ""),
                   lang, OUT / "thumbnail.jpg")
    print("done", flush=True)


if __name__ == "__main__":
    main()
