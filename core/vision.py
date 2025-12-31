# core/vision.py
from PIL import Image

def _crop_inset(img: Image.Image, inset_ratio=0.02):
    w, h = img.size
    dx = int(w * inset_ratio)
    dy = int(h * inset_ratio)
    if dx <= 0 and dy <= 0:
        return img
    return img.crop((dx, dy, w - dx, h - dy))

def dhash(img: Image.Image, hash_size=16):
    img = img.convert("L")
    img = _crop_inset(img, inset_ratio=0.02)
    img = img.resize((hash_size + 1, hash_size), Image.BILINEAR)

    px = list(img.getdata())
    stride = hash_size + 1

    hval = 0
    for r in range(hash_size):
        row = r * stride
        for c in range(hash_size):
            left = px[row + c]
            right = px[row + c + 1]
            hval = (hval << 1) | (1 if left > right else 0)

    return hval, hash_size * hash_size

def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()

def similarity_dhash(a: Image.Image, b: Image.Image, hash_size=16) -> float:
    ha, n = dhash(a, hash_size)
    hb, _ = dhash(b, hash_size)
    d = hamming(ha, hb)
    return 1.0 - d / n


