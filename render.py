import asyncio
import glob
import json
import os
import random
import shutil
import subprocess
import sys
import re
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import edge_tts
import numpy as np
import requests

W, H, FPS = 1920, 1080, 30
# Slightly boosted colour so cartoon / nature footage looks lively
VF = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
      f"eq=saturation=1.18:contrast=1.04,fps={FPS},format=yuv420p")
Q = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
     "-maxrate", "4500k", "-bufsize", "9000k", "-pix_fmt", "yuv420p"]
OV = 0.35            # cross-dissolve length between clips (seconds)
MIN_CLIP = 4.0       # a clip stays on screen at least this long (seconds)
MAX_CLIPS = 4        # max clips per scene

DEFAULT_VOICE = "bn-BD-NabanitaNeural"
FALLBACK_VOICES = ["bn-BD-PradeepNeural", "bn-IN-BashkarNeural"]

raw = os.environ.get("PLAN") or os.environ.get("SCENES_DATA") or "{}"
try:
    plan = json.loads(raw)
except Exception:
    plan = {}

if isinstance(plan, list):
    plan = {"scenes": plan}
elif isinstance(plan, dict) and "plan" in plan and isinstance(plan["plan"], list):
    plan = {"scenes": plan["plan"]}

plan.setdefault("voice", DEFAULT_VOICE)
plan.setdefault("rate", "+0%")
plan.setdefault("pitch", "")
plan.setdefault("music", "calm")
plan.setdefault("target_seconds", 0)
scenes = plan.get("scenes", [])
if not scenes:
    sys.exit("No scenes found in plan")

work = Path("work")
out = Path("out")
output_dir = Path("output")
work.mkdir(exist_ok=True)
out.mkdir(exist_ok=True)
output_dir.mkdir(exist_ok=True)


def run(cmd):
    cmd = [str(c) for c in cmd]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"FFmpeg error: {res.stderr[-400:]}", flush=True)
        raise subprocess.CalledProcessError(res.returncode, cmd)


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def has_video(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and "video" in r.stdout


# ---------------------------------------------------------------- voice
SR = 48000


async def tts(text, path, voice, rate, pitch):
    voice_list = [voice] + [v for v in FALLBACK_VOICES if v != voice]
    for v in voice_list:
        for attempt in range(3):
            try:
                kw = {"rate": rate}
                if pitch and attempt == 0:
                    kw["pitch"] = pitch
                await edge_tts.Communicate(text, v, **kw).save(str(path))
                if path.exists() and path.stat().st_size > 500:
                    return
            except Exception as e:
                print("TTS retry:", v, attempt, e, flush=True)
                await asyncio.sleep(1)
    raise RuntimeError("TTS failed for text: " + text[:40])


SENT_SPLIT = re.compile(r"(?<=[\u0964.!?\u2026])\s+")


def split_sentences(text):
    """One TTS request per sentence = natural intonation and real pauses."""
    parts = [p.strip() for p in SENT_SPLIT.split(text.strip()) if p.strip()]
    merged = []
    for p in parts:
        if merged and len(merged[-1]) < 22:
            merged[-1] += " " + p
        else:
            merged.append(p)
    out = []
    for p in merged:
        while len(p) > 230:
            cut = max(p.rfind(",", 0, 230), p.rfind(" ", 0, 230))
            if cut < 60:
                cut = 230
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out or [text]


# Studio-style clean-up of one synthetic sentence: trim both ends, remove
# rumble, tame the boxy low-mids, add presence, gentle compression.
CLEAN_AF = (
    "silenceremove=start_periods=1:start_threshold=-46dB:start_silence=0.03,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-46dB:start_silence=0.05,"
    "areverse,"
    "highpass=f=85,"
    "equalizer=f=230:t=q:w=1:g=-1.5,"
    "equalizer=f=3400:t=q:w=1.1:g=2.5,"
    "acompressor=threshold=0.1:ratio=2.5:attack=6:release=120:makeup=1.8,"
    "afade=t=in:d=0.015,alimiter=limit=0.95"
)


def read_wav(path):
    with wave.open(str(path), "rb") as wf:
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    return data.astype(np.float32) / 32768.0


def write_wav(path, mono):
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def make_voice(args):
    """Stage 1: narration -> one clean mono wav (sentences + natural pauses)."""
    i, scene = args
    narration = (scene.get("narration") or scene.get("script") or "").strip()
    raw_wav = work / f"va{i}.wav"
    if not narration:
        write_wav(raw_wav, np.zeros(int(SR * 4.0), dtype=np.float32))
        return raw_wav
    chunks = []
    sentences = split_sentences(narration)
    for k, sent in enumerate(sentences):
        mp3 = work / f"a{i}_{k}.mp3"
        asyncio.run(tts(sent, mp3, plan["voice"], plan["rate"], plan["pitch"]))
        wav = work / f"a{i}_{k}.wav"
        run(["ffmpeg", "-y", "-i", mp3, "-af", CLEAN_AF, "-ac", "1", "-ar", SR,
             "-c:a", "pcm_s16le", wav])
        chunks.append(read_wav(wav))
        last = sent.rstrip()[-1:]
        gap = 0.34 if last in "?!" else 0.28
        if k == len(sentences) - 1:
            gap = 0.45
        chunks.append(np.zeros(int(SR * gap), dtype=np.float32))
    write_wav(raw_wav, np.concatenate(chunks))
    return raw_wav


def make_voice_safe(args):
    try:
        return make_voice(args)
    except Exception as e:
        print(f"Voice error scene {args[0]}: {e}", flush=True)
        return None


def atempo_chain(f):
    return f"atempo={f:.4f}"


# ---------------------------------------------------------------- video
def download(url, path):
    if not url or not str(url).startswith("http"):
        return False
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(2):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if has_video(path):
                return True
        except Exception:
            pass
    return False


def make_segment(src_clip, dur, dst, final=False):
    enc = Q if final else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "17",
                           "-pix_fmt", "yuv420p"]
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src_clip,
         "-t", f"{dur:.3f}", "-an", "-vf", VF] + enc + ["-g", "60", dst])


def make_color(dur, dst):
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"color=c=0x1d4ed8:s={W}x{H}:r={FPS}", "-t", f"{dur:.3f}"]
        + Q + [dst])


def join_segments(segs, base, dst, tag):
    """Cross-dissolve the clips of one scene (falls back to a hard cut)."""
    try:
        cmd = ["ffmpeg", "-y"]
        for s in segs:
            cmd += ["-i", s]
        parts, prev = [], "[0:v]"
        for k in range(1, len(segs)):
            lab = f"[x{k}]"
            parts.append(f"{prev}[{k}:v]xfade=transition=fade:duration={OV}:"
                         f"offset={base * k:.3f}{lab}")
            prev = lab
        cmd += ["-filter_complex", ";".join(parts), "-map", prev] + Q + ["-r", FPS, dst]
        run(cmd)
    except subprocess.CalledProcessError:
        lst = work / f"l{tag}.txt"
        lst.write_text("".join(f"file '{s.name}'\n" for s in segs))
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst] + Q + [dst])


def build_scene(args):
    i, scene, voice_wav, tempo, extra = args
    try:
        wav = work / f"a{i}.wav"
        af = (atempo_chain(tempo) + "," if abs(tempo - 1.0) > 0.01 else "")
        af += f"apad=pad_dur={extra:.3f}"
        run(["ffmpeg", "-y", "-i", voice_wav, "-af", af, "-ac", "2", "-ar", SR,
             "-c:a", "pcm_s16le", wav])
        d = duration(wav)

        urls = []
        if scene.get("clip_url"):
            urls.append(scene["clip_url"])
        for c in (scene.get("clips") or scene.get("video_clips") or []):
            if isinstance(c, str):
                urls.append(c)
            elif isinstance(c, dict):
                urls.append(c.get("url") or c.get("link"))

        clips = []
        for k, url in enumerate(urls[:MAX_CLIPS]):
            if url:
                p = work / f"c{i}_{k}.mp4"
                if download(url, p):
                    clips.append(p)

        n = max(1, min(len(clips), int(d // MIN_CLIP))) if clips else 1
        clips = clips[:n]
        base = d / n
        segs = []
        for k in range(n):
            s = work / f"s{i}_{k}.mp4"
            seg_len = base + (OV if k < n - 1 else 0)
            try:
                if clips:
                    make_segment(clips[k], seg_len, s, final=(n == 1))
                else:
                    make_color(seg_len, s)
            except subprocess.CalledProcessError:
                make_color(seg_len, s)
            segs.append(s)

        vid = work / f"v{i}.mp4"
        if n == 1:
            shutil.copy(segs[0], vid)
        else:
            join_segments(segs, base, vid, i)

        scene_out = work / f"scene{i:03d}.mkv"
        run(["ffmpeg", "-y", "-i", vid, "-i", wav,
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "pcm_s16le",
             "-t", f"{d:.3f}", scene_out])
        print(f"Scene {i + 1} completed", flush=True)
        return scene_out
    except Exception as e:
        print(f"Scene {i} error: {e}", flush=True)
        return None


# ---------------------------------------------------------------- music


def _freq(midi):
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def _add(buf, start, sig):
    n = len(buf)
    start %= n
    end = start + len(sig)
    if end <= n:
        buf[start:end] += sig
    else:
        cut = n - start
        buf[start:] += sig[:cut]
        rest = sig[cut:]
        while len(rest):
            m = min(len(rest), n)
            buf[:m] += rest[:m]
            rest = rest[m:]


def _pluck(f, dur, bright=1.0, decay=6.0):
    t = np.arange(int(SR * dur)) / SR
    s = (np.sin(2 * np.pi * f * t)
         + 0.35 * bright * np.sin(2 * np.pi * 2 * f * t)
         + 0.15 * bright * np.sin(2 * np.pi * 3 * f * t))
    env = np.exp(-t * decay)
    env[:int(SR * 0.004)] *= np.linspace(0, 1, int(SR * 0.004))
    return s * env


def synth_music(style, dst):
    """Original, royalty-free music loop made with numpy (no downloads)."""
    kids = style == "kids"
    bpm = 116 if kids else 72
    beat = 60.0 / bpm
    root = 60  # C4
    # C - Am - F - G, two bars each, 8 bars in total
    bars = [(0, [0, 4, 7]), (9, [0, 3, 7]), (5, [0, 4, 7]), (7, [0, 4, 7])] * 2
    n_total = int(SR * beat * 4 * len(bars))
    buf = np.zeros(n_total)
    rng = np.random.default_rng(7)

    for b, (off, chord) in enumerate(bars):
        bar_start = int(SR * beat * 4 * b)
        base_note = root + off - (12 if off >= 7 else 0)
        notes = [base_note + c for c in chord]
        if kids:
            pattern = [0, 1, 2, 1, 0, 1, 2, 1]
            for k, idx in enumerate(pattern):
                note = notes[idx] + (12 if k % 4 == 2 else 0)
                _add(buf, bar_start + int(SR * beat * 0.5 * k),
                     0.30 * _pluck(_freq(note + 12), 0.9, 1.0, 7.0))
                if k % 2 == 1:  # light shaker on the off beat
                    n = int(SR * 0.05)
                    tick = rng.standard_normal(n) * np.exp(-np.arange(n) / (SR * 0.012))
                    _add(buf, bar_start + int(SR * beat * 0.5 * k), 0.035 * tick)
            for k in (0, 2):  # bouncy bass
                _add(buf, bar_start + int(SR * beat * k),
                     0.34 * _pluck(_freq(base_note - 12), 0.7, 0.3, 5.0))
            if b % 2 == 1:  # little melody on the last beat
                _add(buf, bar_start + int(SR * beat * 3),
                     0.22 * _pluck(_freq(notes[2] + 24), 0.8, 1.2, 5.0))
        else:
            for k in range(4):
                note = notes[[0, 1, 2, 1][k]]
                _add(buf, bar_start + int(SR * beat * k),
                     0.26 * _pluck(_freq(note + 12), 1.6, 0.7, 2.6))
            t = np.arange(int(SR * beat * 4)) / SR
            pad = sum(np.sin(2 * np.pi * _freq(n_ - 12) * t) for n_ in notes) / len(notes)
            swell = np.sin(np.pi * t / t[-1]) ** 2
            _add(buf, bar_start, 0.20 * pad * swell)

    buf = buf / (np.max(np.abs(buf)) + 1e-9) * 0.85
    d = int(SR * 0.013)  # tiny stereo width
    left = buf
    right = np.concatenate([buf[-d:], buf[:-d]])
    stereo = np.stack([left, right], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(str(dst), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def get_music(style):
    files = []
    for ext in ("mp3", "wav", "m4a", "ogg"):
        files += glob.glob(f"music/*.{ext}")
    if files:
        return random.choice(files)
    dst = work / f"music_{style}.wav"
    synth_music(style, dst)
    return str(dst)


# ---------------------------------------------------------------- main
# Stage 1: voices for every scene
with ThreadPoolExecutor(max_workers=4) as pool:
    voices = list(pool.map(make_voice_safe, list(enumerate(scenes))))

durs = [duration(v) if v else 0.0 for v in voices]
voice_total = sum(durs)
target = float(plan.get("target_seconds") or 0)
tempo, extra = 1.0, 0.0
if target > 0 and voice_total > 0:
    tempo = min(max(voice_total / target, 0.90), 1.20)
    gap = target - voice_total / tempo
    if gap > 0:
        extra = min(gap / max(len(scenes), 1), 1.6)
print(f"Voice total {voice_total:.0f}s, target {target:.0f}s -> tempo {tempo:.3f}, "
      f"extra pause {extra:.2f}s per scene", flush=True)

# Stage 2: pictures + sound per scene
jobs = [(i, s, voices[i], tempo, extra) for i, s in enumerate(scenes) if voices[i]]
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(build_scene, jobs))

scene_files = [r for r in results if r]
if not scene_files:
    sys.exit("Video rendering failed on all scenes")

concat_list = work / "all.txt"
concat_list.write_text("".join(f"file '{f.name}'\n" for f in scene_files))
allmkv = work / "all.mkv"
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", allmkv])

total = duration(allmkv)
music = get_music(plan.get("music", "calm"))

# Voice is cleaned and levelled, the music "ducks" under the voice, and the
# final mix is limited and normalised to -16 LUFS (YouTube-friendly loudness).
fade_start = max(total - 3.0, 0)
fc = (
    "[0:a]highpass=f=80,asplit=2[vo][sc];"
    "[1:a]volume=0.55,afade=t=in:d=1.5[mus];"
    "[mus][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[duck];"
    "[vo][duck]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[raw];"
    f"[raw]alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=11,"
    f"afade=t=out:st={fade_start:.2f}:d=3[mix]"
)
mix = work / "mix.wav"
run(["ffmpeg", "-y", "-i", allmkv, "-stream_loop", "-1", "-i", music,
     "-filter_complex", fc, "-map", "[mix]", "-t", f"{total:.2f}",
     "-c:a", "pcm_s16le", "-ar", "48000", mix])

final_primary = out / "final.mp4"
final_compat = output_dir / "final_video.mp4"

run(["ffmpeg", "-y", "-i", allmkv, "-i", mix,
     "-map", "0:v:0", "-map", "1:a:0",
     "-c:v", "copy",
     "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
     "-shortest", "-movflags", "+faststart", final_primary])

shutil.copy(final_primary, final_compat)
print(f"Video rendered successfully with audio track: {final_primary}")
