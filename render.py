import os
import json
import asyncio
import requests
import subprocess
import edge_tts

# ভয়েস অপশন: বাংলা প্রাকৃতিক কণ্ঠ
VOICE = "bn-BD-PradeepNeural"

async def generate_speech(text, output_file):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)

def get_media_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        val = float(result.stdout.strip())
        return val if val > 0 else 5.0
    except Exception:
        return 5.0

def download_file(url, target_path):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=45)
        if r.status_code == 200:
            with open(target_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
            return True
    except Exception as e:
        print(f"Failed to download {url}: {e}")
    return False

def main():
    raw_data = os.environ.get("SCENES_DATA", "[]")
    try:
        data = json.loads(raw_data)
        if isinstance(data, dict):
            scenes = data.get("scenes") or data.get("plan") or []
        else:
            scenes = data
    except Exception as e:
        print(f"Error parsing SCENES_DATA: {e}")
        scenes = []

    if not scenes:
        print("No scenes found to render!")
        return

    os.makedirs("temp", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    concat_list = []
    
    for i, scene in enumerate(scenes):
        print(f"--- Processing Scene {i + 1}/{len(scenes)} ---")
        narration = scene.get("narration") or scene.get("script") or ""
        
        # ক্লিপের লিঙ্ক খুঁজে বের করা
        clip_url = scene.get("clip_url")
        if not clip_url and scene.get("clips"):
            clip_url = scene["clips"][0].get("url") or scene["clips"][0].get("link")

        audio_path = f"temp/audio_{i}.mp3"
        raw_clip_path = f"temp/raw_clip_{i}.mp4"
        processed_clip_path = f"temp/scene_{i}.mp4"

        # ১. ভয়েস জেনারেট করা
        if narration.strip():
            try:
                asyncio.run(generate_speech(narration, audio_path))
                target_duration = get_media_duration(audio_path) + 0.3
            except Exception as e:
                print(f"TTS Error on scene {i}: {e}")
                target_duration = 5.0
                subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(target_duration), audio_path])
        else:
            target_duration = 5.0
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(target_duration), audio_path])

        # ২. ভিডিও ফুটেজ হ্যান্ডলিং ও সাইজিং
        has_video = False
        if clip_url and download_file(clip_url, raw_clip_path):
            has_video = True

        if has_video:
            # ক্লিপটিকে অডিওর দৈর্ঘ্যে লুপ করে স্কেল করা (1080p @ 30fps)
            filter_chain = (
                "scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080,"
                "fps=30,"
                "format=yuv420p"
            )
            cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1", "-i", raw_clip_path,
                "-i", audio_path,
                "-t", str(target_duration),
                "-vf", filter_chain,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-shortest", processed_clip_path
            ]
        else:
            # ফুটেজ না পেলে মসৃণ গ্রেডিয়েন্ট ডার্ক ব্যাকগ্রাউন্ড তৈরি করা
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=0x111827:s=1920x1080:r=30",
                "-i", audio_path,
                "-t", str(target_duration),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-shortest", processed_clip_path
            ]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        concat_list.append(f"file '{os.path.abspath(processed_clip_path)}'\n")

    # ৩. সব দৃশ্য একসাথে জোড়া লাগানো
    list_file_path = "temp/concat.txt"
    with open(list_file_path, "w") as f:
        f.writelines(concat_list)

    final_output = "output/final_video.mp4"
    stitch_cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file_path,
        "-c", "copy", final_output
    ]
    subprocess.run(stitch_cmd)
    print("All scenes stitched perfectly into output/final_video.mp4")

if __name__ == "__main__":
    main()
