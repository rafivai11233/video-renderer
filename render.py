import asyncio
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import edge_tts
import requests

# রেজোলিউশন ও অপ্টিমাইজড ফ্রেম রেট
W, H, FPS = 1280, 720, 30
VF = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},format=yuv420p"
FADE = 0.20

DEFAULT_VOICE = "bn-BD-PradeepNeural"
FALLBACK_VOICES = ["bn-BD-NabanitaNeural", "bn-IN-BashkarNeural"]

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
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


async def tts(text, path, voice, rate):
    voice_list = [voice] + [v for v in FALLBACK_VOICES if v != voice]
    for v in voice_list:
        for attempt in range(3):
            try:
                await edge_tts.Communicate(text, v, rate=rate).save(str(path))
                if path.exists() and path.stat().st_size > 500:
                    return
            except Exception:
                await asyncio.sleep(1)
    raise RuntimeError("TTS failed: " + text[:40])


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


def make_segment(src, dur, dst, fade_in, fade_out):
    vf = VF
    if fade_in:
        vf += f",fade=t=in:st=0:d={FADE}"
    if fade_out:
        vf += f",fade=t=out:st={max(dur - FADE, 0):.2f}:d={FADE}"
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src,
         "-t", f"{dur:.2f}", "-an", "-vf", vf, "-c:v", "libx264",
         "-preset", "ultrafast", "-crf", "26", "-g", "60", dst])


def make_color(dur, dst):
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"color=c=0x0f172a:s={W}x{H}:r={FPS}", "-t", f"{dur:.2f}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
         "-crf", "26", dst])


def build_scene(args):
    i, scene = args
    try:
        narration = scene.get("narration") or scene.get("script") or ""
        mp3 = work / f"a{i}.mp3"
        
        if narration.strip():
            asyncio.run(tts(narration, mp3, plan["voice"], plan["rate"]))
        else:
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "4.0", mp3])

        d = duration(mp3) + 0.35

        urls = []
        if scene.get("clip_url"):
            urls.append(scene["clip_url"])
        raw_clips = scene.get("clips") or scene.get("video_clips") or []
        for c in raw_clips:
            if isinstance(c, str):
                urls.append(c)
            elif isinstance(c, dict):
                urls.append(c.get("url") or c.get("link"))

        clips = []
        for k, url in enumerate(urls[:2]):
            if url:
                p = work / f"c{i}_{k}.mp4"
                if download(url, p):
                    clips.append(p)

        n = max(len(clips), 1)
        seg_len = d / n
        segs = []
        for k in range(n):
            s = work / f"s{i}_{k}.mp4"
            try:
                if clips:
                    make_segment(clips[k], seg_len, s, k == 0, k == n - 1)
                else:
                    make_color(seg_len, s)
            except subprocess.CalledProcessError:
                make_color(seg_len, s)
            segs.append(s)

        lst = work / f"l{i}.txt"
        lst.write_text("".join(f"file '{s.name}'\n" for s in segs))
        vid = work / f"v{i}.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", vid])

        scene_out = work / f"scene{i:03d}.mkv"
        run(["ffmpeg", "-y", "-i", vid, "-i", mp3,
             "-map", "0:v", "-map", "1:a", "-c:v", "copy",
             "-af", "aresample=48000,apad", "-ac", "2", "-c:a", "pcm_s16le",
             "-t", f"{d:.2f}", scene_out])
        print(f"Scene {i+1} completed", flush=True)
        return scene_out
    except Exception as e:
        print(f"Scene {i} error: {e}", flush=True)
        return None


def get_music():
    files = []
    for ext in ("mp3", "wav", "m4a", "ogg"):
        files += glob.glob(f"music/*.{ext}")
    if files:
        return random.choice(files)
    pad = work / "pad.wav"
    expr = ("0.22*sin(2*PI*110*t)+0.15*sin(2*PI*164.81*t)+0.12*sin(2*PI*220*t)"
            "+0.08*sin(2*PI*277.18*t)+0.06*sin(2*PI*329.63*t)")
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"aevalsrc={expr}:s=48000:d=60",
         "-af", "lowpass=f=900,afade=t=in:d=2,afade=t=out:st=58:d=2",
         "-ac", "2", pad])
    return str(pad)


# মাল্টি-কোর সমান্তরাল রেন্ডারিং (দ্রুত রেন্ডার করার জন্য)
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(build_scene, list(enumerate(scenes))))

scene_files = [r for r in results if r]
if not scene_files:
    sys.exit("Video rendering failed on all scenes")

concat_list = work / "all.txt"
concat_list.write_text("".join(f"file '{f.name}'\n" for f in scene_files))
allmkv = work / "all.mkv"
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", allmkv])

total = duration(allmkv)
music = get_music()

fc = (
    "[0:a]highpass=f=75,acompressor=threshold=0.1:ratio=3:attack=5:release=120:makeup=2[vo];"
    "[vo]asplit=2[vo1][sc];"
    "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.22[mus];"
    "[mus][sc]sidechaincompress=threshold=0.03:ratio=8:attack=15:release=600[duck];"
    "[vo1][duck]amix=inputs=2:duration=first:weights='1 0.85':normalize=0[mix]"
)
mix = work / "mix.wav"
run(["ffmpeg", "-y", "-i", allmkv, "-stream_loop", "-1", "-i", music,
     "-filter_complex", fc, "-map", "[mix]", "-t", f"{total:.2f}",
     "-c:a", "pcm_s16le", "-ar", "48000", mix])

final_primary = out / "final.mp4"
final_compat = output_dir / "final_video.mp4"

run(["ffmpeg", "-y", "-i", allmkv, "-i", mix,
     "-map", "0:v", "-map", "1:a",
     "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=out:st={max(total - 2, 0):.2f}:d=2",
     "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
     "-movflags", "+faststart", "-t", f"{total:.2f}", final_primary])

shutil.copy(final_primary, final_compat)
print("Rendering finished perfectly in record time!")
