"""產生 PWA 圖示（純 Python PNG 寫入器，免額外依賴）。"""
import struct
import zlib
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"


def png(width, height, pixel_fn, path):
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes(pixel_fn(x, y))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + \
        chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")
    path.write_bytes(out)
    print(f"wrote {path} ({len(out)} bytes)")


BG = (13, 18, 32, 255)
GREEN = (34, 197, 94, 255)
RED = (239, 68, 68, 255)
AMBER = (245, 158, 11, 255)


def make(size, path):
    u = size / 192.0  # 基準 192
    # 三根 K 棒（綠、紅、綠）＋ 底部橘色基線
    candles = [  # (中心x, 影線頂, 影線底, 實體頂, 實體底, 色)
        (58, 52, 140, 72, 118, GREEN),
        (96, 38, 120, 52, 96, RED),
        (134, 30, 126, 44, 104, GREEN),
    ]
    bw, ww = 22, 5  # 實體寬、影線寬

    def px(x, y):
        gx, gy = x / u, y / u
        for cx, wt, wb, bt, bb, col in candles:
            if abs(gx - cx) <= ww / 2 and wt <= gy <= wb:
                out = col
                break
            if abs(gx - cx) <= bw / 2 and bt <= gy <= bb:
                out = col
                break
        else:
            out = AMBER if 156 <= gy <= 162 and 34 <= gx <= 158 else BG
        return out

    png(size, size, px, path)


if __name__ == "__main__":
    make(192, DOCS / "icon-192.png")
    make(512, DOCS / "icon-512.png")
