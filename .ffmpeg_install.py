import os, sys, zipfile, shutil, urllib.request, subprocess

DL = r'C:\Users\91311\.workbuddy\binaries\ffmpeg_dl'
ZIP = os.path.join(DL, 'ffmpeg.zip')
EXTRACT = r'C:\Users\91311\.workbuddy\binaries\ffmpeg'
BIN = r'C:\Users\91311\bin'

URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
EXPECT_MIN = 160_000_000  # ~162MB

os.makedirs(DL, exist_ok=True)
os.makedirs(EXTRACT, exist_ok=True)
os.makedirs(BIN, exist_ok=True)

# 1) download in THIS process (no cross-process file visibility issue)
print("==> downloading ffmpeg (single process) ...")
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=300) as resp, open(ZIP, "wb") as f:
    total = 0
    while True:
        chunk = resp.read(1024 * 1024)
        if not chunk:
            break
        f.write(chunk)
        total += len(chunk)
        if total % (20 * 1024 * 1024) < 1024 * 1024:
            print("    downloaded %.1f MB" % (total / 1e6))
print("    total bytes:", total)
assert total >= EXPECT_MIN, "download too small: %d" % total

# 2) extract in SAME process
print("==> extracting ...")
with zipfile.ZipFile(ZIP) as zf:
    zf.extractall(EXTRACT)
binroot = None
for root, _, files in os.walk(EXTRACT):
    if os.path.basename(root) == 'bin' and any(f.endswith('.exe') for f in files):
        binroot = root
        break
assert binroot, "ffmpeg bin dir not found after extraction"
print("    bin dir:", binroot)

# 3) copy exes to PATH dir
print("==> copying exes to", BIN)
for exe in ['ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe']:
    s = os.path.join(binroot, exe)
    if os.path.exists(s):
        shutil.copy(s, os.path.join(BIN, exe))
        print("    copied", exe, os.path.getsize(os.path.join(BIN, exe)), "bytes")

# 4) verify
r = subprocess.run([os.path.join(BIN, 'ffmpeg.exe'), '-version'],
                  capture_output=True, text=True)
print("==> ffmpeg -version exit:", r.returncode)
print(r.stdout.splitlines()[0] if r.stdout else r.stderr[:200])
print("DONE")
