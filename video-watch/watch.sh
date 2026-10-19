#!/usr/bin/env bash
# StarNet Video Watch — العميل (يشغّله Claude من مساحة العمل)
# الاستخدام: watch.sh URL [mode=watch|listen|see] [max_frames=16] [model=large-v3-turbo] [lang=auto]
# يطبع مسار مجلد النتيجة في آخر سطر. احذفه بعد التحليل بـ: watch.sh --clean JOB_DIR
set -euo pipefail
REPO="${VW_REPO:-Mhsn732/starnet-media}"
API="https://api.github.com/repos/$REPO"
H=(-H "Accept: application/vnd.github+json" -H "Content-Type: application/json")
OUT_ROOT="${VW_OUT:-/tmp/video-watch}"

if [[ "${1:-}" == "--clean" ]]; then
  dir="$2"; job="$(basename "$dir")"
  # حذف الفرع يتم داخل GitHub عبر workflow التنظيف
  curl -sS -X POST "${H[@]}" "$API/actions/workflows/video-watch-clean.yml/dispatches" \
    -d "{\"ref\":\"main\",\"inputs\":{\"job_id\":\"$job\"}}" -o /dev/null -w "clean dispatch: %{http_code}\n" || true
  rm -rf "$dir"; echo "cleaned $job"; exit 0
fi

URL="$1"; MODE="${2:-watch}"; FRAMES="${3:-16}"; MODEL="${4:-large-v3-turbo}"; LANG_="${5:-auto}"
JOB="$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
body=$(python3 -c 'import json,sys; print(json.dumps({"ref":"main","inputs":dict(zip(["url","job_id","mode","max_frames","model","lang"],sys.argv[1:]))}))' \
  "$URL" "$JOB" "$MODE" "$FRAMES" "$MODEL" "$LANG_")
code=$(curl -sS -X POST "${H[@]}" "$API/actions/workflows/video-watch.yml/dispatches" -d "$body" -o /tmp/vw_dispatch.txt -w "%{http_code}")
[[ "$code" == "204" ]] || { echo "dispatch failed ($code):"; cat /tmp/vw_dispatch.txt; exit 1; }
echo "dispatched job $JOB" >&2

deadline=$(( $(date +%s) + ${VW_TIMEOUT:-2400} ))
while :; do
  if git ls-remote --exit-code "https://github.com/$REPO" "refs/heads/watch/$JOB" >/dev/null 2>&1; then break; fi
  # كشف فشل الـ run قبل ما يرفع النتيجة
  st=$(curl -sS "${H[@]}" "$API/actions/workflows/video-watch.yml/runs?per_page=10" \
     | python3 -c 'import json,sys; j=sys.argv[1]
d=json.load(sys.stdin)
for r in d.get("workflow_runs",[]):
    if j in (r.get("display_title") or ""): print(r["status"], r.get("conclusion") or "", r["html_url"]); break' "$JOB" 2>/dev/null || true)
  [[ -n "$st" ]] && echo "  run: $st" >&2
  if [[ "$st" == completed* ]]; then sleep 8; git ls-remote --exit-code "https://github.com/$REPO" "refs/heads/watch/$JOB" >/dev/null 2>&1 && break; echo "run finished without result: $st"; exit 1; fi
  (( $(date +%s) > deadline )) && { echo "timeout waiting for job $JOB"; exit 1; }
  sleep 15
done

mkdir -p "$OUT_ROOT"
git clone -q --depth 1 --single-branch -b "watch/$JOB" "https://github.com/$REPO" "$OUT_ROOT/$JOB"
rm -rf "$OUT_ROOT/$JOB/.git"
echo "$OUT_ROOT/$JOB"
