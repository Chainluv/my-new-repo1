"""Finishing pass for the "MAY I" music video, one segment at a time.

The house style (grade, grain, halation, transition looks) lives in this file
and stays fixed across segments. Each segment gets a JSON plan with its cuts,
beat-synced camera moves and transition points (see plans/seg01.json).

Every source frame stays in place; audio is stream-copied untouched.

Usage: python3 finish.py INPUT.mp4 OUTPUT.mp4 plans/segXX.json
"""
import subprocess, sys, math, json
import numpy as np, cv2
from multiprocessing import Pool

SRC, DST, PLAN_PATH = sys.argv[1], sys.argv[2], sys.argv[3]
W, H = 1920, 1080
FPS = 30000 / 1001

PLAN = json.load(open(PLAN_PATH))
CUTS = PLAN["cuts"]                      # first frame of each shot, plus total frame count
N = CUTS[-1]                             # decode() checks this against the source
SOFTEN = {int(k): v for k, v in PLAN.get("soften", {}).items()}
GRAIN_SEED = 1000 + PLAN.get("global_start_frame", 0)   # keeps grain unique across segments


def shot_of(f):
    for i in range(len(CUTS) - 1):
        if CUTS[i] <= f < CUTS[i + 1]:
            return i
    return len(CUTS) - 2


def ease_io(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def ramp(t, a, b, s0, s1):
    """Eased push from scale s0 at frame a to s1 at frame b (held outside)."""
    return s0 + (s1 - s0) * ease_io((t - a) / (b - a))


def punch(t, f0, amp, attack=2.0, release=9.0):
    """Quick punch-in landing on beat frame f0, then a smooth ease back."""
    if t < f0 - attack:
        return 1.0
    if t < f0:
        return 1 + amp * ease_out((t - (f0 - attack)) / attack)
    return 1 + amp * (1 - ease_io((t - f0) / release))


def snapback(t, f0, amp, dur=6.0):
    """Shot opens punched-in at f0 and snaps back to full frame."""
    if t < f0:
        return 1.0
    return 1 + amp * (1 - ease_out((t - f0) / dur))


def hold(t, sc):
    """Constant zoom, e.g. to continue the previous segment's framing or hide black edges."""
    return sc


MOVES = {"ramp": ramp, "punch": punch, "snapback": snapback, "hold": hold}


def motion(t):
    """(scale, anchor_x, anchor_y) for frame t; t may be fractional for motion blur."""
    f = int(math.floor(t + 1e-6))
    t = max(t, CUTS[shot_of(max(f, 0))])  # never sample across a cut
    for grp in PLAN["camera"]:
        a, b = grp["frames"]
        if a <= f <= b:
            sc = 1.0
            for m in grp["moves"]:
                sc *= MOVES[m[0]](t, *m[1:])
            return sc, grp["anchor"][0], grp["anchor"][1]
    return 1.0, W / 2, H / 2


# Transition envelopes (house style): offsets from the plan frame -> strength.
SHAPES = {
    "flash": {-3: .12, -2: .3, -1: .65, 0: 1.0, 1: .55, 2: .25, 3: .1},
    "whip":  {-2: .25, -1: .6, 0: 1.0, 1: .85, 2: .4, 3: .15},
    "leak":  {-4: .1, -3: .25, -2: .5, -1: .85, 0: 1.0, 1: .7, 2: .4, 3: .2, 4: .08},
}
RGB_DECAY = [1.0, 0.5, 0.22, 0.1]
TABLES = {"flash": {}, "whip": {}, "leak": {}, "rgb": {}}
for tr in PLAN.get("transitions", []):
    for off, v in SHAPES[tr["type"]].items():
        TABLES[tr["type"]][tr["frame"] + off] = v * tr.get("strength", 1.0)
for f0, amp in PLAN.get("rgb_hits", []):
    for i, d in enumerate(RGB_DECAY):
        TABLES["rgb"][f0 + i] = max(TABLES["rgb"].get(f0 + i, 0), amp * d)
FLASH, WHIP, LEAK, RGB = (TABLES[k] for k in ("flash", "whip", "leak", "rgb"))


def env(f, table):
    return table.get(f, 0.0)


def build_curve():
    x = np.linspace(0, 1, 4096, dtype=np.float32)
    s = 1 / (1 + np.exp(-6.0 * (x - 0.45)))
    s = (s - s[0]) / (s[-1] - s[0])
    y = 0.5 * x + 0.5 * s                       # gentle S for density
    y = np.where(y > 0.72, 0.72 + (y - 0.72) * (1 - 0.35 * ((y - 0.72) / 0.28)), y)  # soft shoulder
    y = y / y[-1]
    return 0.022 + (0.955 - 0.022) * y              # rich but not crushed blacks, rolled-off whites


CURVE = build_curve()
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
R2 = ((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2
VIGNETTE = (1 - 0.11 * np.clip(R2 / 2, 0, 1) ** 1.1)[..., None].astype(np.float32)
del yy, xx, R2
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)

FRAMES = None
SHOT_GAIN = None
SHOT_WB = None


def warp(img, sc, ax, ay):
    if abs(sc - 1) < 1e-4:
        return img.astype(np.float32)
    M = np.float32([[sc, 0, ax * (1 - sc)], [0, sc, ay * (1 - sc)]])
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LANCZOS4,
                          borderMode=cv2.BORDER_REFLECT).astype(np.float32)


def shift_x(ch, dx):
    if dx == 0:
        return ch
    M = np.float32([[1, 0, dx], [0, 1, 0]])
    return cv2.warpAffine(ch, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def render(f):
    src = FRAMES[f]
    s = shot_of(f)

    # 1. Camera move with 180-degree shutter motion blur on fast changes.
    sc1, ax, ay = motion(f)
    sc0, _, _ = motion(f - 0.5)
    if abs(sc1 - sc0) > 0.0015:
        n = 6
        acc = np.zeros((H, W, 3), np.float32)
        for k in range(n):
            u = k / (n - 1)
            acc += warp(src, sc0 + (sc1 - sc0) * u, ax, ay)
        img = acc / n
    else:
        img = warp(src, sc1, ax, ay)
    img /= 255.0

    if s in SOFTEN:
        img = cv2.GaussianBlur(img, (0, 0), SOFTEN[s])

    # 2. Directional (whip) blur transition.
    w = env(f, WHIP)
    if w > 0:
        L = int(6 + 48 * w) | 1
        k = np.ones((1, L), np.float32) / L
        img = cv2.addWeighted(img, 1 - min(1, w * 1.2), cv2.filter2D(img, -1, k), min(1, w * 1.2), 0)

    # 3. Grade: per-shot exposure + white-balance match, film curve, muted colour.
    img = img * SHOT_GAIN[s]
    img = img + SHOT_WB[s]
    img = np.clip(img, 0, 1)
    img = np.interp(img, np.linspace(0, 1, 4096), CURVE).astype(np.float32)
    L = img @ LUMA
    sat = 0.84 - 0.14 * np.clip((L - 0.65) / 0.3, 0, 1)
    img = L[..., None] + sat[..., None] * (img - L[..., None])
    sh = np.clip(1 - L / 0.4, 0, 1)[..., None]          # shadows: hint of teal
    hi = np.clip((L - 0.55) / 0.45, 0, 1)[..., None]    # highlights: hint of warmth
    img += sh * np.float32([-0.008, 0.002, 0.010]) + hi * np.float32([0.014, 0.004, -0.012])

    # 4. Restrained halation around bright areas.
    small = cv2.resize(img, (W // 4, H // 4), interpolation=cv2.INTER_AREA)
    Ls = small @ LUMA
    m = np.clip((Ls - 0.74) / 0.22, 0, 1) ** 1.5
    m = cv2.GaussianBlur(m, (0, 0), 5)
    m = cv2.resize(m, (W, H), interpolation=cv2.INTER_LINEAR)[..., None]
    img += m * np.float32([0.085, 0.030, 0.012])

    # 5. Light leak + film flash transitions.
    fl = env(f, FLASH)
    lk = env(f, LEAK)
    for strength, cx, col in ((fl, 1.05 - 0.5 * fl, (1.0, 0.62, 0.30)),
                              (lk, 0.95 - 0.45 * lk, (1.0, 0.45, 0.22))):
        if strength > 0:
            gx = np.linspace(0, 1, W // 8, dtype=np.float32)[None, :]
            gy = np.linspace(0, 1, H // 8, dtype=np.float32)[:, None]
            blob = np.exp(-(((gx - cx) / 0.32) ** 2 + ((gy - 0.4) / 0.6) ** 2))
            blob = cv2.resize(blob, (W, H), interpolation=cv2.INTER_CUBIC)[..., None]
            leak = blob * np.float32(col) * 0.5 * strength
            img = 1 - (1 - np.clip(img, 0, 1)) * (1 - leak)        # screen blend
            flash = max(0.0, strength - 0.5) * 2 * 0.22            # brief overexposure at peak
            if flash > 0:
                img = img + (np.float32([1.0, 0.95, 0.88]) - img) * flash

    # 6. RGB separation on hits and transitions.
    r = env(f, RGB)
    if r > 0:
        dx = 9 * r
        img[..., 0] = shift_x(np.ascontiguousarray(img[..., 0]), dx)
        img[..., 2] = shift_x(np.ascontiguousarray(img[..., 2]), -dx)

    img *= VIGNETTE

    # 7. Fine, luminance-weighted film grain (mostly mono, a touch of chroma).
    rng = np.random.default_rng(GRAIN_SEED + f)
    g = rng.standard_normal((H, W), dtype=np.float32)
    g = cv2.GaussianBlur(g, (0, 0), 0.65)
    c = rng.standard_normal((H // 2, W // 2, 3), dtype=np.float32)
    c = cv2.resize(cv2.GaussianBlur(c, (0, 0), 0.7), (W, H), interpolation=cv2.INTER_LINEAR)
    L = np.clip(img @ LUMA, 0, 1)
    amt = (0.012 + 0.028 * (4 * L * (1 - L)))[..., None]
    img += amt * (g[..., None] * 0.85 + c * 0.25)

    return (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()


def decode():
    cmd = ["ffmpeg", "-v", "error", "-i", SRC, "-vf",
           "scale=in_color_matrix=bt709:in_range=tv:flags=accurate_rnd+full_chroma_int",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3)
    assert len(fr) == N, f"source has {len(fr)} frames, plan says {N}"
    return fr


def shot_stats(frames):
    gains, wbs = [], []
    target = 0.50
    for i in range(len(CUTS) - 1):
        sub = frames[CUTS[i]:CUTS[i + 1]:3, ::8, ::8].astype(np.float32) / 255
        lum = float(np.median(sub @ LUMA))
        gains.append(float(np.clip((target / max(lum, 0.02)) ** 0.4, 0.9, 1.12)))
        mean = sub.reshape(-1, 3).mean(0)
        wbs.append(((mean.mean() - mean) * 0.3).astype(np.float32))  # 30% toward neutral
    # Graphics/title cards keep their own exposure and colour.
    for i in PLAN.get("no_match", []):
        gains[i], wbs[i] = 1.0, np.zeros(3, np.float32)
    # A shot that continues from the previous segment reuses that segment's values.
    for k, v in PLAN.get("match_override", {}).items():
        gains[int(k)], wbs[int(k)] = float(v["gain"]), np.float32(v["wb"])
    return gains, wbs


def main():
    global FRAMES, SHOT_GAIN, SHOT_WB
    FRAMES = decode()
    SHOT_GAIN, SHOT_WB = shot_stats(FRAMES)
    for i, (g, wb) in enumerate(zip(SHOT_GAIN, SHOT_WB)):
        print(f"shot {i}: frames {CUTS[i]}-{CUTS[i+1]-1} gain {g:.3f} wb {np.round(wb, 4)}")
    enc = subprocess.Popen([
        "ffmpeg", "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", "30000/1001", "-i", "-",
        "-i", SRC, "-map", "0:v", "-map", "1:a", "-c:a", "copy",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuv420p",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-tune", "grain", "-profile:v", "high",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
        "-movflags", "+faststart", DST], stdin=subprocess.PIPE)
    with Pool(4) as pool:
        for i, buf in enumerate(pool.imap(render, range(N), chunksize=2)):
            enc.stdin.write(buf)
            if i % 30 == 0:
                print("frame", i, flush=True)
    enc.stdin.close()
    enc.wait()


if __name__ == "__main__":
    main()
