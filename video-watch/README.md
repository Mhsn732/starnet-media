# Video Watch (StarNet)

يخلي Claude "يشاهد" أي فيديو (يوتيوب، تيك توك، إنستغرام، فيسبوك، X …) عبر GitHub Actions:

- **yt-dlp** ينزّل الفيديو مؤقتاً (بدون تسجيل دخول)
- **ffmpeg** يستخرج فريمات عند القطعات + توزيع منتظم، مع طباعة التوقيت على كل فريم
- **faster-whisper** (large-v3-turbo) يفرّغ الكلام مع التوقيت
- الفيديو والصوت ينحذفوا فوراً؛ النتيجة (فريمات + نص + meta.json) ترفع على فرع مؤقت `watch/<job>` ينحذف بعد التحليل

## التشغيل من Claude
```bash
video-watch/watch.sh "URL" [watch|listen|see] [max_frames] [model] [lang]
video-watch/watch.sh --clean /tmp/video-watch/<job>   # حذف النتيجة محلياً + حذف الفرع عبر workflow التنظيف (وفي تنظيف يومي احتياطي)
```
