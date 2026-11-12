"""把录制帧合成 README 用的 GIF（零外部依赖：只用 Pillow）。

用法::

    python scripts/demo/build_gif.py
    python scripts/demo/build_gif.py --frames docs/assets/demo-frames --out docs/assets/demo.gif --width 1100

处理逻辑
--------
1. 按文件名排序读取 ``frame-*.png``；
2. **去掉连续重复帧**（流式输出时页面可能没变化），保留最后一次；
3. 等比缩放到目标宽度，转成 128 色调色板（GIF 体积可控）；
4. 按 ``--duration`` 毫秒/帧写出，``--hold`` 指定最后几帧的停留时间（让结尾不闪）。
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PIL import Image


def _digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def build(frames_dir: Path, out: Path, *, width: int, duration: int, hold_ms: int, hold_frames: int) -> None:
    files = sorted(frames_dir.glob("frame-*.png"))
    if not files:
        raise SystemExit(f"{frames_dir} 里没有 frame-*.png，先跑 record_demo.mjs")

    # 连续相同帧只保留一张（体积能小一半以上）
    kept: list[Path] = []
    last = ""
    for file in files:
        digest = _digest(file)
        if digest != last:
            kept.append(file)
            last = digest

    images: list[Image.Image] = []
    for file in kept:
        image = Image.open(file).convert("RGB")
        ratio = width / image.width
        height = max(2, round(image.height * ratio))
        images.append(image.resize((width, height), Image.LANCZOS))

    durations = [duration] * len(images)
    for index in range(max(0, len(durations) - hold_frames), len(durations)):
        durations[index] = hold_ms

    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        out,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"✓ {out}  ({len(files)} 帧 → {len(images)} 帧, {width}px, {size_mb:.2f} MB)")
    if size_mb > 10:
        print("⚠ 超过 GitHub 建议的 10MB：降低 --width 或增大 --duration/减少帧")


def main() -> None:
    parser = argparse.ArgumentParser(description="帧序列 → GIF")
    parser.add_argument("--frames", default="docs/assets/demo-frames")
    parser.add_argument("--out", default="docs/assets/demo.gif")
    parser.add_argument("--width", type=int, default=1100)
    parser.add_argument("--duration", type=int, default=260, help="每帧毫秒（默认 260 ≈ 4fps）")
    parser.add_argument("--hold-ms", type=int, default=1600, help="最后几帧停留毫秒")
    parser.add_argument("--hold-frames", type=int, default=3)
    args = parser.parse_args()

    build(Path(args.frames), Path(args.out), width=args.width, duration=args.duration,
          hold_ms=args.hold_ms, hold_frames=args.hold_frames)


if __name__ == "__main__":
    main()