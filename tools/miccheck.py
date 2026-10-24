import json, subprocess, numpy as np
# Запуск: scp tools/miccheck.py <host>:/tmp/ && ssh <host> "docker run --rm -v $DATA_DIR:/data:ro -v /tmp/miccheck.py:/app/m.py:ro podcast-bot:local python3 /app/m.py /data/jobs/<папка выпуска>"
import sys
J = sys.argv[1]                     # папка выпуска внутри контейнера, например /data/jobs/<№>_<дата>
meta, tr, sync = (json.load(open(f"{J}/{n}.json")) for n in ("meta", "transcript", "sync"))
for p in meta["tracks"]:
    print(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_sample,bit_rate:format=bit_rate",
                          "-of", "compact", p], capture_output=True, text=True).stdout.strip().replace("\n", " | "))
L, s = sync["offset"], sync["drift"]
ws = sorted((w["start"] / 1000, w["end"] / 1000, str(w["channel"])) for w in tr["words"] if 100 < w["start"] / 1000 < 1300)
def solo(ch):
    return [(a, b) for a, b, c in ws if c == ch and b - a > 0.25 and not any(c2 != ch and a2 < b + 0.3 and b2 > a - 0.3 for a2, b2, c2 in ws)]
bands = [(40, 80), (80, 120), (120, 160), (160, 250), (250, 400), (400, 800), (800, 2000), (2000, 4000), (4000, 8000),
         (8000, 12000), (12000, 14000), (14000, 16000), (16000, 20000)]
def load(path, a, b, ch_map=None):
    x = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a}", "-to", f"{b}", "-i", path, "-f", "f32le", "-ar", "48000", "-"],
                       capture_output=True).stdout
    return np.frombuffer(x, np.float32)
for idx, (path, ch, nm) in enumerate(((meta["tracks"][0], "1", meta["names"][0]), (meta["tracks"][1], "2", meta["names"][1]))):
    nch = 2 if idx == 0 else 1
    acc = np.zeros(len(bands)); lr = []
    for a, b in solo(ch)[:300]:
        if idx == 1:
            a, b = L + (1 + s) * a, L + (1 + s) * b
        x = load(path, a, b).reshape(-1, nch)
        if len(x) < 4096:
            continue
        if nch == 2:
            lr.append((np.sqrt(np.mean(x[:, 0] ** 2)), np.sqrt(np.mean(x[:, 1] ** 2)), np.corrcoef(x[:, 0], x[:, 1])[0, 1]))
        m = x.mean(1)
        F = np.abs(np.fft.rfft(m * np.hanning(len(m)))) ** 2; f = np.fft.rfftfreq(len(m), 1 / 48000)
        acc += [F[(f >= lo) & (f < hi)].sum() / max(1, ((f >= lo) & (f < hi)).sum()) for lo, hi in bands]   # плотность на Гц
    d = 10 * np.log10(acc + 1e-20); d -= d.max()
    print(nm, "· слов:", len(solo(ch)[:300]))
    print("   " + "  ".join(f"{lo}-{hi}:{v:.0f}" for (lo, hi), v in zip(bands, d)))
    if lr:
        lr = np.array(lr); print(f"   стерео: L {20*np.log10(lr[:,0].mean()):.1f} дБ, R {20*np.log10(lr[:,1].mean()):.1f} дБ, корреляция L/R {lr[:,2].mean():.2f}")
