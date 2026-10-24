import podcast as P, json, numpy as np, subprocess, shutil, os
# Запуск: scp tools/eqcheck.py <host>:/tmp/ && ssh <host> "docker run --rm -v $DATA_DIR:/data -e PODCAST_SETTINGS=/data/settings.json -v /tmp/eqcheck.py:/app/e.py:ro podcast-bot:local python3 /app/e.py /data/jobs/<папка выпуска>"
import sys
JOB = sys.argv[1]                     # папка выпуска внутри контейнера, например /data/jobs/<№>_<дата>
W = "/data/tmp_eq"
os.makedirs(W, exist_ok=True)
for f in ("sync", "transcript", "meta", "plan", "levels"):
    shutil.copy(f"{JOB}/{f}.json", W)
res = {}
for eq in (True, False):
    c = P.load_cfg(W); c["assets"] = "/nonexistent"; c["eq"] = eq
    P.render(W, c, preview=True)
    mp3 = W + "/preview/" + json.load(open(W + "/preview.json"))["file"]
    res[eq] = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", mp3, "-ac", "1", "-ar", "44100", "-f", "f32le", "-"],
                                           capture_output=True).stdout, np.float32)
plan = json.load(open(W + "/plan.json")); S = plan["start"]; tr = json.load(open(W + "/transcript.json"))
ws = sorted((w["start"] / 1000, w["end"] / 1000, str(w["channel"])) for w in tr["words"] if S <= w["start"] / 1000 < S + 60)
def solo(ch):
    return [(a - S, b - S) for a, b, c in ws if c == ch and not any(c2 != ch and a2 < b + 0.3 and b2 > a - 0.3 for a2, b2, c2 in ws)]
bands = [(80, 160), (160, 400), (400, 1000), (1000, 2500), (2500, 5000), (5000, 8000), (8000, 12000), (12000, 18000)]
def spec(x, segs):
    acc = np.zeros(len(bands))
    for a, b in segs:
        seg = x[int(a * 44100):int(b * 44100)]
        if len(seg) < 2048:
            continue
        F = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2; f = np.fft.rfftfreq(len(seg), 1 / 44100)
        acc += [F[(f >= lo) & (f < hi)].sum() for lo, hi in bands]
    return 10 * np.log10(acc + 1e-12)
print("полосы, Гц:  " + "  ".join(f"{lo}-{hi}" for lo, hi in bands))
names = json.load(open(W + "/meta.json"))["names"]
for ch, nm in (("1", names[0]), ("2", names[1])):
    sg = solo(ch); on, off = spec(res[True], sg), spec(res[False], sg)
    print(f"{nm}: сольных слов {len(sg)}")
    print("  без EQ:     " + "  ".join(f"{v - off.max():6.0f}" for v in off))
    print("  с пресетом: " + "  ".join(f"{v - on.max():6.0f}" for v in on))
shutil.rmtree(W)
