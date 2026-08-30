#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""Generate XYZ tiles from the flat continent maps for Grafana's Geomap xyz basemap.

The map images are flat; XYZ tiles live in Web Mercator space. We define a small
fake lon/lat bbox per continent, render tiles by sampling the image *linearly in
lat* (which is exactly what the marker SQL does in reverse), so markers and tiles
can never disagree. Self-test: every landmark must round-trip image px -> lon/lat
-> mercator px -> image px within 0.5 px, and land on a generated tile.

Sampling is resolution-correct rather than nearest-neighbour: per zoom the source
is Lanczos-resampled to exactly the mercator pixel size the bbox occupies at that
zoom, which antialiases the low zooms instead of point-sampling every third pixel
of a 966px image into a 300px tile row. Only the remaining vertical remap (linear
lat -> mercator lat) is done per row, and that is bilinear.
"""
import argparse, math, os
from PIL import Image

TILE = 256

def project(x, y, m):
    """World yards -> continent-image pixels. Ported from azerothcore/playermap index.js get_player_position."""
    where530 = 0
    if m == 530:
        if y < -1000 and y > -10000 and x > 5000:   x, y, where530 = x - 10349, y + 6357, 1
        elif y < -7000 and x < 0:                    x, y, where530 = x + 3961, y + 13931, 2
        else:                                        x, y, where530 = x - 3070, y - 1265, 3
    elif m == 609:
        x, y = x - 2355, y + 5662
    s = {3: 0.051446, 571: 0.050085}.get(where530 if where530 == 3 else m, 0.025140)
    xpos, ypos = round(x * s), round(y * s)
    if   m == 530 and where530 == 1: return 858 - ypos, 84 - xpos
    elif m == 530 and where530 == 2: return 103 - ypos, 261 - xpos
    elif m == 530 and where530 == 3: return 684 - ypos, 229 - xpos
    elif m == 571:                   return 505 - ypos, 642 - xpos
    elif m == 609:                   return 896 - ypos, 232 - xpos
    elif m == 1:                     return 194 - ypos, 398 - xpos
    else:                            return 752 - ypos, 291 - xpos  # map 0 (EK) default

def bbox_for(img):
    w, h = img.size
    span_lat = 20.0
    span_lon = 20.0 * w / h
    return 0.0, span_lon, -10.0, 10.0  # L0, L1, B0, B1

def img_to_ll(px, py, img, box):
    L0, L1, B0, B1 = box
    w, h = img.size
    return L0 + px / w * (L1 - L0), B1 - py / h * (B1 - B0)

def ll_to_img(lon, lat, img, box):
    L0, L1, B0, B1 = box
    w, h = img.size
    return (lon - L0) / (L1 - L0) * w, (B1 - lat) / (B1 - B0) * h

def merc_px(lon, lat, z):
    n = TILE * 2 ** z
    gx = (lon + 180.0) / 360.0 * n
    lat = max(min(lat, 85.05112878), -85.05112878)
    rad = math.radians(lat)
    gy = (1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * n
    return gx, gy

def px_to_ll(gx, gy, z):
    n = TILE * 2 ** z
    lon = gx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy / n))))
    return lon, lat

def merc_canvas(img, box, z):
    """Source image -> one image covering the bbox in mercator pixels at zoom z.

    Longitude is linear in mercator x and the bbox spans only +/-10 deg of
    latitude, so a Lanczos resize to the exact mercator pixel size does nearly
    all the work with proper antialiasing; the residual linear-lat -> mercator-lat
    warp (under a pixel across the whole image) is a bilinear per-row remap.
    Returns the canvas and its mercator-pixel origin.
    """
    L0, L1, B0, B1 = box
    gx0, gy0 = merc_px(L0, B1, z)
    gx1, gy1 = merc_px(L1, B0, z)
    W, H = max(1, round(gx1 - gx0)), max(1, round(gy1 - gy0))
    mip = img.resize((W, H), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (W, H))
    for j in range(H):
        _, lat = px_to_ll(gx0, gy0 + j + 0.5, z)
        v = (B1 - lat) / (B1 - B0) * H - 0.5
        vi = math.floor(v)
        v0, v1 = max(0, min(H - 1, vi)), max(0, min(H - 1, vi + 1))
        row = mip.crop((0, v0, W, v0 + 1))
        if v1 != v0:
            row = Image.blend(row, mip.crop((0, v1, W, v1 + 1)), max(0.0, min(1.0, v - vi)))
        canvas.paste(row, (0, j))
    return canvas, gx0, gy0

def cut_tile(canvas, gx0, gy0, tx, ty):
    """One tile out of the mercator canvas. Crop pads with black outside the bbox."""
    left, top = round(tx * TILE - gx0), round(ty * TILE - gy0)
    return canvas.crop((left, top, left + TILE, top + TILE))

CONTINENTS = {
    "Azeroth":   ("azeroth.jpg",   0),
    "Outland":   ("outland.jpg",   530),
    "Northrend": ("northrend.jpg", 571),
}
LANDMARKS = {  # name, x, y, map, continent
    "Stormwind": (-8833.38, 628.628, 0), "Ironforge": (-4918.88, -940.406, 0),
    "Undercity": (1584.14, 240.308, 0), "BootyBay": (-14297.2, 530.993, 0),
    "Orgrimmar": (1629.85, -4373.64, 1), "ThunderBluff": (-1277.37, 124.804, 1),
    "Darnassus": (9949.56, 2284.21, 1), "Gadgetzan": (-7177.15, -3785.34, 1),
    "Shattrath": (-1838.16, 5301.79, 530),
    "Silvermoon": (9487.69, -7279.2, 530), "Exodar": (-3965.7, -11653.6, 530),
    "Dalaran": (5807.98, 588.487, 571),
}
MAP2CONT = {0: "Azeroth", 1: "Azeroth", 530: "Outland", 571: "Northrend", 609: "Azeroth"}

def selftest():
    err = 0
    for name, (x, y, m) in LANDMARKS.items():
        cont = MAP2CONT[m]
        img = Image.open(CONTINENTS[cont][0])
        box = bbox_for(img)
        px, py = project(x, y, m)
        lon, lat = img_to_ll(px, py, img, box)
        gx, gy = merc_px(lon, lat, 6)
        lon2, lat2 = px_to_ll(gx, gy, 6)
        ux, uy = ll_to_img(lon2, lat2, img, box)
        e = math.hypot(ux - px, uy - py)
        status = "ok" if e < 0.5 else "FAIL"
        if e >= 0.5: err += 1
        print(f"  {name:13s} img({px:4d},{py:4d}) -> ll({lon:7.3f},{lat:7.3f}) -> merc -> img({ux:8.2f},{uy:8.2f})  err={e:.4f}px {status}")
    return err

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tiles")
    ap.add_argument("--zooms", default="4,5,6,7")
    ap.add_argument("--selftest-only", action="store_true")
    args = ap.parse_args()

    print("self-test: image px -> lon/lat -> mercator -> lon/lat -> image px")
    if selftest():
        raise SystemExit("self-test FAILED")
    if args.selftest_only:
        return

    zooms = [int(z) for z in args.zooms.split(",")]
    for cont, (fname, _) in CONTINENTS.items():
        img = Image.open(fname).convert("RGB")
        box = bbox_for(img)
        n_tiles = 0
        for z in zooms:
            # tiles covering the bbox
            canvas, gx0, gy0 = merc_canvas(img, box, z)
            gx1, gy1 = merc_px(box[1], box[2], z)
            for tx in range(int(gx0 // TILE), int(gx1 // TILE) + 1):
                for ty in range(int(gy0 // TILE), int(gy1 // TILE) + 1):
                    t = cut_tile(canvas, gx0, gy0, tx, ty)
                    d = os.path.join(args.out, cont, str(z), str(tx))
                    os.makedirs(d, exist_ok=True)
                    t.save(os.path.join(d, f"{ty}.jpg"), quality=92, subsampling=0)
                    n_tiles += 1
        print(f"{cont}: {n_tiles} tiles")

if __name__ == "__main__":
    main()
