"""将 weibo_data/manual/ 下的扁平文件按 screen_name 归入子目录"""
import os, re, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
MANUAL = os.path.join(BASE, "weibo_data", "manual")
if not os.path.isdir(MANUAL):
    print(f"ERROR: {MANUAL} not found")
    exit()

moved = 0
for fname in os.listdir(MANUAL):
    src = os.path.join(MANUAL, fname)
    if not os.path.isfile(src) or not fname.endswith(".md"):
        continue
    # 文件名格式: 单条_{screen_name}_{date}_{wid}.md
    m = re.match(r"单条_(.+?)_(\d{8})_(\d+)", fname)
    if not m:
        print(f"[SKIP] {fname}")
        continue
    screen_name = m.group(1)
    # 清理 screen_name 用于目录名
    safe_dir = re.sub(r'[<>:"/\\|?*]', '_', screen_name)
    dst_dir = os.path.join(MANUAL, safe_dir)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, fname)
    shutil.move(src, dst)
    moved += 1

print(f"Done: reorganized {moved} files")
