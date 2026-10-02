#!/usr/bin/env python3
"""AICenter Reels renderer v3 (GitHub Actions; can also run locally for tests).

The site sends only a script (scenes: hook / 3 points / cta, each with on-screen text and spoken Hebrew).
Everything heavy happens here, never on the site's shared hosting:
  1. voice: each scene's Hebrew line -> WAV through the site's TTS relay (or a local file in test mode)
  2. frames: Chromium (Playwright) draws brand-styled layers with real web fonts: a background, kinetic title states
     (words appear one by one), highlighted caption chunks, all 1080x1920 with transparency
  3. ffmpeg: slow zoom on the background, the title states and captions timed to the voice, a progress bar,
     crossfades between scenes, loudness -14 LUFS, H.264/AAC, faststart
Usage on Actions: python render.py            (job from the site, video uploaded back)
Local test:       python render.py --spec job.json --audio-dir DIR --out reel.mp4
"""
import argparse, html, json, os, re, subprocess, sys, urllib.request

API = "https://aicenter.co.il/api/reels-job.php"
TTS = "https://aicenter.co.il/api/reels-tts.php"
TOKEN = os.environ.get("SITE_TOKEN", "")
FPS, XF = 30, 0.25
TEMPO = 1.08  # a touch faster than the raw voice: Reels pace
W, H = 1080, 1920

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Frank+Ruhl+Libre:wght@700;900&family=Heebo:wght@500;700;800;900&display=block');
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1080px;height:1920px;background:transparent;direction:rtl;font-family:Heebo,Arial,sans-serif;overflow:hidden}
.bg{position:absolute;inset:0;background:#F9F6EF}
.blob{position:absolute;border-radius:50%;filter:blur(90px);opacity:.55}
.grid{position:absolute;inset:0;background-image:radial-gradient(rgba(163,71,39,.13) 2.2px,transparent 2.4px);background-size:64px 64px}
.safe{position:absolute;left:90px;right:90px;top:300px;bottom:680px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:34px}
.kicker{font-weight:800;font-size:42px;color:#FFFDF8;background:#A34727;padding:14px 34px;border-radius:999px;letter-spacing:.5px}
.title{font-family:'Frank Ruhl Libre',serif;font-weight:900;color:#1B1916;line-height:1.08;letter-spacing:-1px}
.title .w{display:inline-block;margin:0 .14em}
.title .w.off{opacity:0}
.title .hl{color:#A34727}
.mark{box-shadow:inset 0 -.2em 0 rgba(163,71,39,.2);border-radius:6px}
.sticker{position:absolute;left:96px;top:300px;font-size:190px;line-height:1;transform:rotate(-12deg);filter:drop-shadow(0 14px 18px rgba(0,0,0,.18));font-family:'Noto Color Emoji','Segoe UI Emoji','Apple Color Emoji',sans-serif}
.sub{font-size:48px;font-weight:500;color:#4A433C;line-height:1.35;max-width:860px}
.num{width:150px;height:150px;border-radius:44px;background:#A34727;color:#FFFDF8;font-weight:900;font-size:86px;display:grid;place-items:center;box-shadow:0 18px 40px rgba(163,71,39,.35);transform:rotate(-6deg)}
.brand{position:absolute;top:150px;left:0;right:0;display:flex;justify-content:center;align-items:center;gap:14px;font-weight:800;font-size:34px;color:#2B2622;opacity:.85;direction:ltr}
.brand .ai{font-family:Heebo,sans-serif;font-weight:700;font-size:24px;color:#4A433C;background:rgba(255,253,248,.75);border:1.5px solid rgba(74,67,60,.25);border-radius:999px;padding:4px 14px;direction:rtl}
.brand i{width:46px;height:46px;border-radius:13px;background:#A34727;color:#fff;font-style:normal;font-family:'Frank Ruhl Libre',serif;font-size:22px;display:grid;place-items:center}
.cap{position:absolute;left:70px;right:70px;top:1270px;display:flex;justify-content:center}
.cap span{font-weight:900;font-size:70px;line-height:1.15;color:#fff;background:rgba(27,25,22,.88);padding:16px 34px;border-radius:26px;text-align:center;box-shadow:0 10px 30px rgba(0,0,0,.18)}
.cap b{color:#1B1916;background:#F2B08C;border-radius:14px;padding:0 10px;margin:0 -4px}
.url{font-weight:900;font-size:58px;color:#FFFDF8;background:#2F5D50;padding:22px 46px;border-radius:999px;direction:ltr}
.send{font-size:46px;font-weight:700;color:#2F5D50}
"""


def http(url, data=None, headers=None, timeout=180):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "aicenter-reels", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def run(cmd):
    subprocess.run(cmd, check=True)


def dur(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())


def words_html(text, visible=None, hl_last=True):
    ws = text.split()
    out = []
    key = max(range(len(ws)), key=lambda i: len(ws[i])) if ws else -1  # highlight the longest word
    for i, w in enumerate(ws):
        cls = "w" + (" off" if visible is not None and i >= visible else "") + (" hl mark" if i == key else "")
        out.append(f'<span class="{cls}">{html.escape(w)}</span>')
    return " ".join(out)


def title_size(text):
    n = len(text)
    return 128 if n <= 16 else 112 if n <= 26 else 98 if n <= 38 else 86


def page_html(body, bg=False, variant=0):
    if bg:
        spots = [("#A34727", 8, 88, 900), ("#2F5D50", 92, 8, 760), ("#E08A5E", 70, 60, 520)]
        sh = (variant * 37) % 100
        blobs = "".join(f'<div class="blob" style="background:{c};width:{s}px;height:{s}px;left:{(x + sh) % 100}%;top:{(y + sh // 2) % 100}%;transform:translate(-50%,-50%)"></div>' for c, x, y, s in spots)
        # left-to-right and fixed positions: in an RTL page the blobs' overflow shifted the whole picture sideways
        return (f'<!doctype html><html dir="ltr"><head><meta charset="utf-8"><style>{CSS}html,body{{direction:ltr;background:#F9F6EF}}'
                f'.bg,.grid,.blob{{position:fixed}}</style></head><body><div class="bg"></div>{blobs}<div class="grid"></div></body></html>')
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{body}</body></html>'


def scene_layers(sc, kicker):
    """Returns (title_states_html[], static_html) for one scene. States: words appear one by one."""
    show = sc["show"].strip()
    n = len(show.split())
    size = title_size(show)
    states = []
    for k in range(1, n + 1):
        if sc["type"] == "hook":
            inner = (f'<div class="kicker">{html.escape(kicker)}</div>' if kicker else "") + f'<div class="title" style="font-size:{size}px">{words_html(show, k)}</div>'
        elif sc["type"] == "point":
            sub = f'<div class="sub" style="opacity:{1 if k == n else 0}">{html.escape(sc.get("sub", ""))}</div>' if sc.get("sub") else ""
            inner = f'<div class="num">{sc.get("num", "")}</div><div class="title" style="font-size:{size - 8}px">{words_html(show, k)}</div>{sub}'
        else:
            extra = '<div class="url">aicenter.co.il</div><div class="send">שלחו למי שצריך את זה</div>' if k == n else '<div class="url" style="opacity:0">aicenter.co.il</div>'
            inner = f'<div class="title" style="font-size:{size}px">{words_html(show, k)}</div>{extra}'
        states.append('<div class="brand"><i>AI</i>AICenter<span class="ai">קריינות AI</span></div><div class="safe">' + inner + "</div>")
    return states


def caption_chunks(say):
    ws = say.split()
    chunks, cur = [], []
    for w in ws:
        cur.append(w)
        if len(cur) >= 3 or (len(cur) >= 2 and re.search(r"[.,!?:]$", w)):
            chunks.append(" ".join(cur)); cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def cap_html(chunk, key):
    ws = chunk.split()
    inner = " ".join(f"<b>{html.escape(w)}</b>" if i == key else html.escape(w) for i, w in enumerate(ws))
    return f'<div class="cap"><span>{inner}</span></div>'


def render_frames(spec, d):
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)

        def shot(html_doc, path, transparent=True):
            pg.set_content(html_doc, wait_until="networkidle")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=path, omit_background=transparent, type="png" if transparent else "jpeg", **({} if transparent else {"quality": 92}))

        for i, sc in enumerate(spec["scenes"]):
            bgp = f"{d}/s{i}-bg.jpg"
            shot(page_html("", bg=True, variant=i), bgp, transparent=False)
            states = []
            for k, st in enumerate(scene_layers(sc, spec.get("kicker", ""))):
                pth = f"{d}/s{i}-t{k}.png"; shot(page_html(st), pth); states.append(pth)
            caps = []
            for k, c in enumerate(caption_chunks(sc.get("text") or sc["say"])):
                for j, w in enumerate(c.split()):
                    pth = f"{d}/s{i}-c{k}-{j}.png"; shot(page_html(cap_html(c, j)), pth); caps.append((pth, w))
            st = None
            if sc.get("emoji"):
                st = f"{d}/s{i}-emoji.png"; shot(page_html(f'<div class="sticker">{html.escape(sc["emoji"])}</div>'), st)
            out.append({"bg": bgp, "states": states, "caps": caps, "sticker": st})
        b.close()
    return out


def tts(text, path, audio_dir=None, idx=0):
    if audio_dir:
        src = os.path.join(audio_dir, f"s{idx}.wav")
        with open(src, "rb") as f, open(path, "wb") as g:
            g.write(f.read())
        return
    data = http(TTS, data=json.dumps({"text": text}).encode(), headers={"X-Reels-Token": TOKEN, "Content-Type": "application/json"})
    if len(data) < 4000:
        raise RuntimeError("tts failed: " + data[:200].decode("utf-8", "ignore"))
    with open(path, "wb") as f:
        f.write(data)


def clean_wav(path):
    """Gemini appends a C2PA provenance block to the raw PCM; played as sound it is a burst of static. Cut it."""
    import wave
    with wave.open(path, "rb") as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    p = frames.find(b"C2PA")
    while p != -1 and b"jumb" not in frames[p:p + 32]:
        p = frames.find(b"C2PA", p + 4)
    if p != -1:
        frames = frames[:p - p % 2]
        with wave.open(path, "wb") as w:
            w.setparams(params)
            w.writeframes(frames)


_WHISPER = []


def word_times(wav):
    """Word timings of the real voice (faster-whisper on the runner's CPU). [] when unavailable: captions fall back to length."""
    try:
        from faster_whisper import WhisperModel
        if not _WHISPER:
            _WHISPER.append(WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8"))
        segs, _ = _WHISPER[0].transcribe(wav, language="he", word_timestamps=True, beam_size=1, vad_filter=False, condition_on_previous_text=False)
        return [(w.start, w.end, w.word.strip()) for sg in segs for w in (sg.words or []) if w.word.strip()]
    except Exception as e:  # noqa: BLE001
        print("whisper off:", e, flush=True)
        return []


def caption_times(words, voice, wt):
    """Start/end of each caption word. The captions are written text, the voice is phonetic Hebrew, so the words are matched by
    their position along the sentence (spoken length), mapped onto the recognised words' real times."""
    def weight(w):  # English letters and digits are spoken longer than written (API = איי פי איי)
        return len(w) + 1.5 * sum(c.isascii() and c.isalnum() for c in w) + 1
    if len(wt) >= 2:
        tot = sum(max(1, len(x[2])) for x in wt)
        pts, acc = [], 0
        for st, en, w in wt:
            pts.append((acc / tot, st)); acc += max(1, len(w)); pts.append((acc / tot, en))
    else:
        pts = [(0.0, 0.1), (1.0, max(0.2, voice - 0.1))]

    def at(f):
        for (f0, t0), (f1, t1) in zip(pts, pts[1:]):
            if f <= f1:
                return t0 if f1 == f0 else t0 + (t1 - t0) * (f - f0) / (f1 - f0)
        return pts[-1][1]
    W = [weight(w) for w in words]
    T, acc, starts = sum(W) or 1, 0, []
    for w in W:
        starts.append(at(acc / T)); acc += w
    ends = starts[1:] + [min(voice + 0.3, at(1.0) + 0.35)]
    return [(a, max(b, a + 0.12)) for a, b in zip(starts, ends)]


def render_scene(i, sc, fr, d, audio_dir):
    au = f"{d}/s{i}.wav"
    tts(sc["say"], au, audio_dir, i)
    clean_wav(au)
    voice = dur(au) / TEMPO
    length = round(voice + 0.5, 2)
    n = len(fr["states"])
    step = min(0.16, 0.9 / max(1, n))
    inputs = ["-loop", "1", "-t", str(length), "-i", fr["bg"]]
    for s in fr["states"]:
        inputs += ["-loop", "1", "-t", str(length), "-i", s]
    for c, _ in fr["caps"]:
        inputs += ["-loop", "1", "-t", str(length), "-i", c]
    if fr.get("sticker"):
        inputs += ["-loop", "1", "-t", str(length), "-i", fr["sticker"]]
    inputs += ["-i", au]
    frames = int(length * FPS) + 2
    f = [f"[0:v]scale=2376:4224,zoompan=z='min(1+0.0011*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}[v0]"]
    last = "v0"
    for k in range(n):  # word-by-word title: each state replaces the previous one
        a = 0.05 + k * step
        b = (0.05 + (k + 1) * step) if k < n - 1 else length + 1
        f.append(f"[{last}][{k + 1}:v]overlay=x=0:y='{"if(lt(t,%.2f),40*pow(1-(t-%.2f)/0.22,2),0)" % (a + 0.22, a) if k == 0 else "0"}':enable='between(t,{a:.2f},{b:.2f})'[v{k + 1}]")
        last = f"v{k + 1}"
    # captions follow the real voice: word times from speech recognition (scaled by the tempo change)
    wt = [(a / TEMPO, b / TEMPO, w) for a, b, w in word_times(au)]
    times = caption_times([c for _, c in fr["caps"]], voice, wt)
    for k, (a, b) in enumerate(times):
        f.append(f"[{last}][{1 + n + k}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[c{k}]")
        last = f"c{k}"
    ai = 1 + n + len(fr["caps"])
    if fr.get("sticker"):  # the sticker drops in with a damped bounce
        f.append(f"[{last}][{ai}:v]overlay=x=0:y='if(lt(t,0.3),-400,50*exp(-9*(t-0.3))*cos(14*(t-0.3)))':enable='gte(t,0.3)'[st]")
        last = "st"; ai += 1
    f.append(f"[{last}]format=yuv420p[vout]")
    f.append(f"[{ai}:a]atempo={TEMPO},afade=t=in:d=0.02,afade=t=out:st={max(0, voice - 0.06):.2f}:d=0.06,aresample=48000,apad=whole_dur={length}[aout]")
    out = f"{d}/scene{i}.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(f), "-map", "[vout]", "-map", "[aout]", "-t", str(length),
         "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", out])
    return out, length


def join(parts, d, out, music=None, gain=0.2):
    inputs, fv, fa = [], [], []
    for p, _ in parts:
        inputs += ["-i", p]
    offset, lv, la = 0.0, "0:v", "0:a"
    for i in range(1, len(parts)):
        offset += parts[i - 1][1] - XF
        fv.append(f"[{lv}][{i}:v]xfade=transition=smoothleft:duration={XF}:offset={offset:.2f}[xv{i}]")
        fa.append(f"[{la}][{i}:a]acrossfade=d={XF}[xa{i}]")
        lv, la = f"xv{i}", f"xa{i}"
    total = sum(p[1] for p in parts) - XF * (len(parts) - 1)
    fv.append(f"color=c=0xA34727:s={W}x12:r={FPS}:d={total:.2f}[pb]")
    fv.append(f"[{lv}][pb]overlay=x='-W+W*t/{total:.2f}':y=0:shortest=1[vout]")
    if music:
        # background music (owner-approved Lyria tracks): looped, well under the voice, ducked further while she speaks
        mi = len(parts)
        inputs += ["-stream_loop", "-1", "-i", music]
        fa.append(f"[{mi}:a]aresample=48000,atrim=0:{total:.2f},volume={gain},afade=t=in:d=0.6,afade=t=out:st={max(0, total - 1.6):.2f}:d=1.5[mus]")
        fa.append(f"[{la}]asplit=2[vo][vk]")
        fa.append("[mus][vk]sidechaincompress=threshold=0.02:ratio=5:attack=30:release=350[duck]")
        fa.append("[vo][duck]amix=inputs=2:duration=first:normalize=0[mx]")
        la = "mx"
    fa.append(f"[{la}]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(fv + fa), "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", out])
    return total


def mark_ai(path):
    """IPTC digital-source-type in the file's XMP: "composite with AI elements" (the voice), read by platforms' AI labelling."""
    import shutil
    if not shutil.which("exiftool"):
        print("exiftool missing: no XMP AI mark", flush=True)
        return
    subprocess.run(["exiftool", "-q", "-overwrite_original",
                    "-XMP-iptcExt:DigitalSourceType=http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia",
                    "-XMP-dc:Description=Narration voice generated with AI (Gemini TTS). AICenter.co.il", path], check=False)


def build(spec, d, out, audio_dir=None):
    os.makedirs(d, exist_ok=True)
    frames = render_frames(spec, d)
    parts = [render_scene(i, sc, frames[i], d, audio_dir) for i, sc in enumerate(spec["scenes"])]
    music = None
    if spec.get("music"):
        music = spec["music"] if os.path.isfile(spec["music"]) else f"{d}/music.mp3"
        if music == f"{d}/music.mp3":
            try:
                with open(music, "wb") as fh:
                    fh.write(http(spec["music"]))
            except Exception as e:  # noqa: BLE001  no music rather than no reel
                print("music download failed:", e, flush=True); music = None
    total = join(parts, d, out, music, float(spec.get("music_gain", 0.2)))
    mark_ai(out)
    print(f"reel {out}: {os.path.getsize(out)} bytes, {total:.1f}s", flush=True)


def upload(job_id, path):
    boundary = "----aicenter" + os.urandom(8).hex()
    with open(path, "rb") as fh:
        video = fh.read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"id\"\r\n\r\n{job_id}\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"reel.mp4\"\r\nContent-Type: video/mp4\r\n\r\n").encode() + video + f"\r\n--{boundary}--\r\n".encode()
    print(http(API, data=body, headers={"X-Reels-Token": TOKEN, "Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300).decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec"); ap.add_argument("--audio-dir"); ap.add_argument("--out", default="reel.mp4")
    a = ap.parse_args()
    if a.spec:
        with open(a.spec, encoding="utf-8") as f:
            build(json.load(f), "work", a.out, a.audio_dir)
        return
    for _ in range(3):
        job = json.loads(http(API + "?v=3", headers={"X-Reels-Token": TOKEN}) or b"{}")
        if not isinstance(job, dict) or not job.get("id"):  # an empty queue answers [] or {}
            print("queue empty"); return
        d = f"job{job['id']}"
        build(job, d, f"{d}/reel.mp4")
        upload(job["id"], f"{d}/reel.mp4")


if __name__ == "__main__":
    main()
