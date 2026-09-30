import os
import json
import subprocess
import requests

def download_file(url, target_path):
    r = requests.get(url, stream=True)
    r.raise_for_status()
    with open(target_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def main():
    os.makedirs("temp", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    raw_data = os.environ.get("SCENES_DATA", "[]")
    scenes = json.loads(raw_data)
    
    concat_list = []
    
    for i, scene in enumerate(scenes):
        idx = scene.get("scene_index", i + 1)
        clips = scene.get("video_clips", [])
        narration = scene.get("narration", "")
        
        # 1. Edge-TTS Audio Generation
        audio_path = f"temp/audio_{idx}.mp3"
        if scene.get("audio_url"):
            download_file(scene["audio_url"], audio_path)
        elif narration:
            cmd = f'edge-tts --voice bn-BD-NabanitaNeural --text "{narration}" --write-media {audio_path}'
            subprocess.run(cmd, shell=True, check=True)
            
        # 2. Download footage
        clip_paths = []
        for c_idx, c_url in enumerate(clips[:2]):
            c_path = f"temp/clip_{idx}_{c_idx}.mp4"
            download_file(c_url, c_path)
            clip_paths.append(c_path)
            
        # 3. Normalize video to 720p (1280x720)
        norm_clip = f"temp/norm_{idx}.mp4"
        if clip_paths:
            cmd = (
                f'ffmpeg -y -i {clip_paths[0]} -vf "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720" '
                f'-c:v libx264 -pix_fmt yuv420p -r 30 -an {norm_clip}'
            )
            subprocess.run(cmd, shell=True, check=True)
            concat_list.append(norm_clip)
            
    # Concat file list
    with open("temp/concat.txt", "w") as f:
        for p in concat_list:
            f.write(f"file '{os.path.abspath(p)}'\n")
            
    # 4. Master and output final video
    final_output = "output/final_video.mp4"
    ffmpeg_cmd = (
        f'ffmpeg -y -f concat -safe 0 -i temp/concat.txt '
        f'-c:v libx264 -preset fast -crf 23 -c:a aac -b:a 256k {final_output}'
    )
    subprocess.run(ffmpeg_cmd, shell=True, check=True)
    print("Render completed successfully!")

if __name__ == "__main__":
    main()
