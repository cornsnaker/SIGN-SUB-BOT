import os
import subprocess
import re
import sys

def run_command(command):
    """Utility to run a system command and return the output."""
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore')
    return result.returncode, result.stdout, result.stderr

def find_ass_track(mkv_path):
    """Scans the MKV file using FFmpeg to find the first English ASS subtitle track."""
    print("Scanning MKV file metadata...")
    code, stdout, stderr = run_command(["ffmpeg", "-i", mkv_path])
    file_info = stderr
    
    # Look for English subtitle streams matching 'ass'
    pattern = r"Stream #0:(\d+)\(eng\): Subtitle: ass"
    matches = re.findall(pattern, file_info)
    
    if not matches:
        # Fallback check: look for any ASS track if 'eng' language tag is missing
        pattern_fallback = r"Stream #0:(\d+): Subtitle: ass"
        matches = re.findall(pattern_fallback, file_info)
        if matches:
            print(f"⚠️  No explicitly 'English' tagged ASS track found. Using first available ASS track: Stream #0:{matches[0]}")
            return matches[0]
        return None
    
    return matches[0]

def extract_and_filter_signs(mkv_path, stream_index):
    """Extracts the subtitle track and strips out the dialogue styles."""
    banned_styles = {"default", "song"}
    
    base_dir = os.path.dirname(os.path.abspath(mkv_path))
    file_name = os.path.splitext(os.path.basename(mkv_path))[0]
    
    temp_ass = os.path.join(base_dir, f"{file_name}_temp_full.ass")
    output_ass = os.path.join(base_dir, f"{file_name}_signs.ass")
    
    print(f"Extracting Stream #0:{stream_index} via FFmpeg...")
    extract_cmd = ["ffmpeg", "-y", "-i", mkv_path, "-map", f"0:{stream_index}", "-c:s", "copy", temp_ass]
    code, stdout, stderr = run_command(extract_cmd)
    
    if code != 0:
        print(f"❌ FFmpeg extraction failed:\n{stderr}")
        return None

    print("Filtering out dialogue tracks to leave only signs/SFX...")
    try:
        with open(temp_ass, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        with open(temp_ass, 'r', encoding='utf-8-sig') as f:
            lines = f.readlines()

    output_lines = []
    in_events_section = False

    for line in lines:
        if "[Events]" in line:
            in_events_section = True
            output_lines.append(line)
            continue
        
        if not in_events_section:
            output_lines.append(line)
            continue

        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) > 3:
                style = parts[3].strip().lower()
                if style not in banned_styles:
                    output_lines.append(line)
        else:
            output_lines.append(line)

    with open(output_ass, 'w', encoding='utf-8') as f:
        f.writelines(output_lines)
    
    if os.path.exists(temp_ass):
        os.remove(temp_ass)
        
    return output_ass

def mux_subtitle_back(mkv_path, ass_path):
    """Muxes only video, audio, English subs, fonts, and the new sign sub track into a new MKV."""
    base_dir = os.path.dirname(os.path.abspath(mkv_path))
    file_name = os.path.splitext(os.path.basename(mkv_path))[0]
    
    final_mkv = os.path.join(base_dir, f"{file_name}_clean_english.mkv")
    
    print("\nMuxing clean English streams back into a new MKV video file...")
    
    mux_cmd = [
        "ffmpeg", "-y",
        "-i", mkv_path,                 # Input 0: Original Video
        "-i", ass_path,                 # Input 1: The new signs-only file
        "-map", "0:v",                  # Include original video
        "-map", "0:a",                  # Include original audio
        "-map", "0:m:language:eng",     # Include ONLY the original subtitles that are tagged English
        "-map", "1:s:0",                # Include our new sign subtitle track
        "-map", "0:t?",                 # Include attachments/fonts if they exist (? makes it optional)
        "-c", "copy",                   # Direct stream copy (no encoding)
        "-metadata:s:s:2", "language=eng", # The new track will be the 3rd subtitle stream (index 2)
        "-metadata:s:s:2", "title=Signs & Songs",
        final_mkv
    ]
    
    code, stdout, stderr = run_command(mux_cmd)
    
    if code == 0:
        print(f"\n🎉 Process Complete!")
        print(f"👉 Generated clean file: {final_mkv}")
        try:
            os.remove(ass_path)
            print("🧹 Cleaned up loose .ass file.")
        except:
            pass
    else:
        print(f"❌ Muxing failed:\n{stderr}")

def main():
    print("======================================================")
    print("  MKV Sign Extractor & Clean English Remuxer   ")
    print("======================================================\n")
    
    mkv_path = input("Drag & drop your .mkv file here and press Enter:\n").strip()
    mkv_path = mkv_path.strip('"').strip("'")
    
    if not os.path.exists(mkv_path):
        print(f"❌ Error: The file path '{mkv_path}' does not exist.")
        return
        
    stream_index = find_ass_track(mkv_path)
    
    if stream_index is None:
        print("❌ Error: Could not find an .ass subtitle track in this file.")
        return
        
    print(f"Found eligible ASS track at Stream #0:{stream_index}")
    
    filtered_ass = extract_and_filter_signs(mkv_path, stream_index)
    
    if filtered_ass and os.path.exists(filtered_ass):
        mux_subtitle_back(mkv_path, filtered_ass)

if __name__ == "__main__":
    main()
