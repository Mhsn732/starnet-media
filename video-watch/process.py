#!/usr/bin/env python3
"""
StarNet Video Watch — يشتغل على سيرفر GitHub Actions.

1. ينزّل الفيديو مؤقتاً بـ yt-dlp (بدون تسجيل دخول)
2. يستخرج الفريمات المهمة بـ ffmpeg (كشف تغيّر المشهد + توزيع منتظم) مع طباعة التوقيت
3. يفرّغ الصوت بـ faster-whisper مع توقيت كل جملة
4. يحذف الفيديو والصوت، ويترك فقط: meta.json + transcript.txt + frames/*.jpg

الاستخدام:
  python process.py URL OUT_DIR --mode watch|listen|see --max-frames 16 --model large-v3-turbo --lang auto
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
ATTEMPTS = []  # سجل محاولات التنزيل (يُحفظ في meta.json للتشخيص)


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def fmt_ts(sec):
    sec = max(0.0, float(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"


def download(url, workdir):
    """ينزّل الفيديو بأقل جودة كافية للتحليل (≤720p) لتسريع العمل."""
    out_tmpl = os.path.join(workdir, "video.%(ext)s")
    base = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--restrict-filenames",
        "-f", "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "--write-info-json", "--retries", "3",
        "-o", out_tmpl,
    ]
    # يوتيوب أحياناً يرفض سيرفرات مراكز البيانات؛ نجرّب واجهات (clients) مختلفة
    attempts = [[]]
    # تيك توك وغيره يرفضوا الطلبات "الآلية"؛ ننتحل بصمة متصفح كروم
    if re.search(r"(tiktok\.com|facebook\.com|fb\.watch)", url):
        attempts.insert(0, ["--impersonate", "chrome"])
    else:
        attempts.append(["--impersonate", "chrome"])
    if re.search(r"(youtube\.com|youtu\.be)", url):
        if os.environ.get("VW_POT_URL"):
            attempts.insert(0, ["--extractor-args",
                                f"youtubepot-bgutilhttp:base_url={os.environ['VW_POT_URL']}"])
        attempts += [
            ["--extractor-args", "youtube:player_client=tv_simply,web_safari"],
            ["--extractor-args", "youtube:player_client=mweb,android_vr"],
            ["--extractor-args", "youtube:player_client=web_embedded,tv"],
        ]
    last_err = None
    for extra in attempts:
        try:
            run(base + extra + [url])
            ATTEMPTS.append({"args": " ".join(extra) or "default", "ok": True})
            last_err = None
            break
        except subprocess.CalledProcessError as e:
            last_err = e
            ATTEMPTS.append({"args": " ".join(extra) or "default", "ok": False,
                             "error": (e.stderr or "").strip()[-500:]})
            print(f"download attempt failed {extra}: {(e.stderr or '')[-400:]}", flush=True)
            for f in os.listdir(workdir):
                os.remove(os.path.join(workdir, f))
    if last_err:
        raise last_err
    video = next(
        os.path.join(workdir, f) for f in os.listdir(workdir)
        if f.startswith("video.") and not f.endswith(".json")
    )
    info = {}
    info_path = os.path.join(workdir, "video.info.json")
    if os.path.exists(info_path):
        with open(info_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for k in ("title", "uploader", "channel", "duration", "upload_date",
                  "view_count", "like_count", "comment_count", "description",
                  "extractor_key", "webpage_url", "width", "height", "fps"):
            if raw.get(k) is not None:
                info[k] = raw[k]
    return video, info


def probe_duration(video):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", video])
    return float(r.stdout.strip() or 0)


def scene_times(video, threshold=0.3):
    """أوقات تغيّر المشهد (القطع) في الفيديو."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", video, "-vf",
         f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
         "-an", "-f", "null", "-"],
        text=True, capture_output=True)
    return [float(t) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)]


def pick_times(duration, scenes, max_frames):
    """يمزج لقطات القطع مع توزيع منتظم، ويضمن أول وآخر الفيديو (الهوك والخاتمة)."""
    if duration <= 0:
        return [0.0]
    times = {0.5 if duration > 1 else 0.0}
    times.add(max(0.0, duration - 0.5))
    # نصف الحصة للقطعات (هي اللحظات اللي يتغير فيها المحتوى)
    if scenes:
        budget = max(1, max_frames // 2)
        step = max(1, len(scenes) // budget)
        times.update(t + 0.15 for t in scenes[::step][:budget])
    # الباقي توزيع منتظم لتغطية الفيديو كله
    remaining = max_frames - len(times)
    if remaining > 0:
        for i in range(1, remaining + 1):
            times.add(duration * i / (remaining + 1))
    times = sorted(t for t in times if t < duration)
    # إزالة اللقطات المتقاربة جداً (< 0.7 ثانية)
    dedup = []
    for t in times:
        if not dedup or t - dedup[-1] >= 0.7:
            dedup.append(t)
    if len(dedup) > max_frames:
        idx = [round(i * (len(dedup) - 1) / (max_frames - 1)) for i in range(max_frames)]
        dedup = [dedup[i] for i in sorted(set(idx))]
    return dedup


def extract_frames(video, times, frames_dir):
    os.makedirs(frames_dir, exist_ok=True)
    font = f"fontfile={FONT}:" if os.path.exists(FONT) else ""
    out = []
    for i, t in enumerate(times, 1):
        label = fmt_ts(t).replace(":", r"\:")
        name = f"f{i:02d}_{fmt_ts(t).replace(':', '-')}.jpg"
        path = os.path.join(frames_dir, name)
        vf = (
            "scale='if(gt(iw,ih),768,-2)':'if(gt(iw,ih),-2,768)',"
            f"drawtext={font}text='{label}':x=12:y=12:fontsize=28:"
            "fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=8"
        )
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
             "-i", video, "-frames:v", "1", "-vf", vf, "-q:v", "4", "-y", path])
        if os.path.exists(path):
            out.append({"file": f"frames/{name}", "t": round(t, 2), "ts": fmt_ts(t)})
    return out


def transcribe(video, workdir, model_name, lang):
    audio = os.path.join(workdir, "audio.wav")
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", audio])
    except subprocess.CalledProcessError:
        return None, [], "no-audio"
    if not os.path.exists(audio) or os.path.getsize(audio) < 2000:
        return None, [], "no-audio"

    # نقرأ الصوت بأنفسنا (wav 16kHz mono) بدل PyAV لتفادي تعارض الإصدارات
    import wave
    import numpy as np
    with wave.open(audio, "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    samples = pcm.astype(np.float32) / 32768.0

    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8",
                         cpu_threads=os.cpu_count() or 4)
    segments, info = model.transcribe(
        samples,
        language=None if lang in ("", "auto") else lang,
        vad_filter=True,
        beam_size=5,
    )
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments]
    return info.language, segs, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--mode", default="watch", choices=["watch", "listen", "see"])
    ap.add_argument("--max-frames", type=int, default=16)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--lang", default="auto")
    a = ap.parse_args()

    a.max_frames = max(2, min(a.max_frames, 40))
    os.makedirs(a.out, exist_ok=True)
    meta = {"url": a.url, "mode": a.mode, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # كل الملفات الثقيلة داخل مجلد مؤقت ينحذف مهما حصل
    workdir = tempfile.mkdtemp(prefix="vw_")
    try:
        t0 = time.time()
        video, info = download(a.url, workdir)
        meta["source"] = info
        meta["download_sec"] = round(time.time() - t0, 1)
        duration = probe_duration(video)
        meta["duration_sec"] = round(duration, 2)

        if a.mode in ("watch", "see"):
            t0 = time.time()
            scenes = scene_times(video)
            meta["scene_cuts"] = len(scenes)
            meta["cuts_per_min"] = round(len(scenes) / (duration / 60), 1) if duration else 0
            times = pick_times(duration, scenes, a.max_frames)
            meta["frames"] = extract_frames(video, times, os.path.join(a.out, "frames"))
            meta["frames_sec"] = round(time.time() - t0, 1)

        if a.mode in ("watch", "listen"):
            t0 = time.time()
            lang, segs, status = transcribe(video, workdir, a.model, a.lang)
            meta["transcript_status"] = status
            meta["language"] = lang
            meta["whisper_model"] = a.model
            meta["transcribe_sec"] = round(time.time() - t0, 1)
            with open(os.path.join(a.out, "transcript.txt"), "w", encoding="utf-8") as fh:
                if not segs:
                    fh.write("(لا يوجد كلام في الفيديو)\n")
                for s in segs:
                    fh.write(f"[{fmt_ts(s['start'])} → {fmt_ts(s['end'])}] {s['text']}\n")
        meta["status"] = "ok"
    except subprocess.CalledProcessError as e:
        meta["status"] = "error"
        meta["error"] = (e.stderr or str(e))[-3000:]
        print(meta["error"], file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        meta["status"] = "error"
        meta["error"] = repr(e)[-3000:]
        print(meta["error"], file=sys.stderr)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # حذف الفيديو والصوت
        meta["download_attempts"] = ATTEMPTS
        with open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in meta.items() if k != "frames"}, ensure_ascii=False, indent=2))
    sys.exit(0 if meta["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
