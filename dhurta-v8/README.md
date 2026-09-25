# Dhurta Media Lite v9

A local Python/Flask YouTube media downloader focused on robust format fallback, accurate download progress, and simple troubleshooting.

## What changed in v9

- Accepts normal YouTube `watch`, `shorts`, `live`, `embed`, `youtu.be`, `m.youtube.com`, and `music.youtube.com` URL forms.
- Normalizes URLs before analysis/download so retry jobs do not incorrectly become `unsupported_url`.
- Resolution buttons are generated from the current analysis only.
- A selected resolution is tried first, followed by MP4-compatible, same-resolution compatible, nearest-lower, and best-compatible fallbacks as appropriate.
- MP4 no longer requires the selected resolution to have an H.264/M4A pair.
- If MP4 is incompatible, the downloader can fall back to MKV instead of failing immediately.
- Download progress comes directly from yt-dlp progress fields and is mapped into a 0–95% download stage; processing/merging is 96–99%; verified final media is 100%.
- Fallback/processing stages are visible in the activity card.
- Only the final media is kept in the managed `Downloads/Dhurta Media` folder; per-job temporary work is isolated under application data and cleaned.
- FFmpeg is the required media engine; FFprobe is diagnostic/optional rather than a hard blocker.
- Cookie-free mode remains the default.
- System Check, Repair Media Engine, Technical Log, and troubleshooting guidance are included.

## Windows

Run `START.bat`. Keep the console window open while Dhurta is running.

If dependencies need repair, run `UPDATE.bat`.

## Recommended default workflow

1. Paste a supported YouTube URL.
2. Analyze.
3. Select an actual resolution button.
4. Keep MP4 unless the source requires a compatibility fallback.
5. Start download.
6. If the selected source is rejected, let Dhurta finish its automatic fallback sequence before retrying manually.

## Troubleshooting

- `format_unavailable`: the source changed or the exact stream disappeared; v9 automatically tries compatible alternatives. Re-analyze if all alternatives fail.
- `unsupported_url`: use a normal YouTube URL; v9 accepts watch/shorts/live/embed/youtu.be forms.
- `ffmpeg`: run System Check, then Repair Media Engine, then restart Dhurta.
- `browser_locked`: leave browser-session mode off. Cookie-free mode is the normal default.
- `youtube_verification` / `po_token`: Dhurta does not bypass YouTube access controls.
- Network errors: retry and reduce concurrent fragments to 1–2.

Dhurta is intended for media you are authorized to download. It does not bypass DRM, paywalls, or access controls.
