import json, logging, os, re, shutil, socket, subprocess, sys, threading, time, uuid, webbrowser, urllib.parse
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_from_directory

logging.getLogger('werkzeug').setLevel(logging.ERROR)

BASE    = Path(__file__).resolve().parent
APPDATA = Path(os.environ.get('LOCALAPPDATA') or (Path.home()/'.local'/'share')) / 'Dhurta Media'
WORK_ROOT = APPDATA / 'jobs'
LOG_ROOT  = APPDATA / 'logs'
CONFIG    = APPDATA / 'config.json'
for p in (WORK_ROOT, LOG_ROOT): p.mkdir(parents=True, exist_ok=True)

app  = Flask(__name__)
LOCK = threading.RLock()
JOBS: dict = {}

# ── Process map: jid → subprocess.Popen  (protected by LOCK) ─────────────
PROCS: dict = {}

DEFAULT = {
    'download_folder_name': 'Dhurta Media',
    'browser': 'none', 'use_cookies': False,
    'quality': 'best', 'container': 'mp4',
    'audio_format': 'mp3', 'audio_quality': '192K',
    'retries': 8, 'fragment_retries': 15,
    'concurrent_fragments': 2, 'sleep_interval': 0, 'max_sleep_interval': 0,
    'rate_limit': '',
    'embed_metadata': True, 'embed_thumbnail': False,
    'embed_subtitles': False, 'subtitle_lang': 'en.*',
    'filename_template': '%(title)s [%(id)s].%(ext)s',
    'auto_cleanup': True,
}
BROWSERS   = ['firefox', 'chrome', 'edge', 'brave', 'opera', 'vivaldi', 'chromium']
MEDIA_EXT  = {'.mp4', '.mkv', '.webm', '.mov', '.m4v', '.mp3', '.m4a',
              '.opus', '.wav', '.flac', '.aac', '.ogg'}

# ── Config ────────────────────────────────────────────────────────────────
def cfg():
    try:
        d = json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.exists() else {}
    except Exception:
        d = {}
    x = DEFAULT.copy()
    x.update({k: v for k, v in d.items() if k in DEFAULT})
    return x

def save_cfg(d):
    x = DEFAULT.copy()
    x.update({k: v for k, v in (d or {}).items() if k in DEFAULT})
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(x, indent=2), encoding='utf-8')
    return x

# ── Download directory ────────────────────────────────────────────────────
def downloads_dir():
    name = str(cfg().get('download_folder_name') or 'Dhurta Media').strip() or 'Dhurta Media'
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            class GUID(ctypes.Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                             ('Data3', wintypes.WORD), ('Data4', wintypes.BYTE*8)]
            g = GUID(0x374DE290, 0x123F, 0x4565,
                     (ctypes.c_ubyte*8)(0x91,0x64,0x39,0xC4,0x92,0x5E,0x46,0x7B))
            p = ctypes.c_wchar_p()
            if (ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(g), 0, None, ctypes.byref(p)) == 0 and p.value):
                d = Path(p.value) / name
                d.mkdir(parents=True, exist_ok=True)
                return d
        except Exception:
            pass
    d = Path.home() / 'Downloads' / name
    d.mkdir(parents=True, exist_ok=True)
    return d

# ── FFmpeg — cached per process ──────────────────────────────────────────
_ffmpeg_cache: str | None = None
_ffmpeg_lock  = threading.Lock()

def candidate_ffmpeg() -> str | None:
    global _ffmpeg_cache
    with _ffmpeg_lock:
        if _ffmpeg_cache is not None:
            # Re-verify cached path still works (in case of env change)
            if Path(_ffmpeg_cache).exists():
                return _ffmpeg_cache
            _ffmpeg_cache = None   # stale — re-search
        vals = []
        env = os.environ.get('DHURTA_FFMPEG')
        if env:
            vals += [Path(env),
                     Path(env) / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')]
        vals += [
            BASE / 'bin' / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'),
            BASE / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'),
        ]
        try:
            import imageio_ffmpeg
            vals.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
        except Exception:
            pass
        w = shutil.which('ffmpeg')
        if w:
            vals.append(Path(w))
        seen: set = set()
        for p in vals:
            try:
                p = p.resolve()
                if p in seen or not p.exists():
                    continue
                seen.add(p)
                r = subprocess.run(
                    [str(p), '-version'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=8,
                )
                if r.returncode == 0:
                    _ffmpeg_cache = str(p)
                    return _ffmpeg_cache
            except Exception:
                continue
        return None

def ffprobe_for(ff: str | None) -> str | None:
    if not ff:
        return None
    p = Path(ff)
    q = p.with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    if q.exists():
        try:
            if subprocess.run([str(q), '-version'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=8).returncode == 0:
                return str(q)
        except Exception:
            pass
    w = shutil.which('ffprobe')
    return w or None

def engine_status() -> dict:
    ff = candidate_ffmpeg()
    fp = ffprobe_for(ff)
    return {
        'available': bool(ff), 'ffmpeg': ff, 'ffprobe': fp,
        'ready': bool(ff),
        'message': 'Ready' if ff else 'FFmpeg unavailable or could not start',
    }

# ── yt-dlp executable ────────────────────────────────────────────────────
def ytdlp_exe() -> str:
    # Prefer the venv-local copy
    p = BASE / '.venv' / ('Scripts/yt-dlp.exe' if os.name == 'nt' else 'bin/yt-dlp')
    if p.exists():
        return str(p)
    # Fall back to system yt-dlp or Python module
    w = shutil.which('yt-dlp')
    return w or 'yt-dlp'

# ── URL normalisation ─────────────────────────────────────────────────────
_YT_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}
_VID_RE   = re.compile(r'^[A-Za-z0-9_-]{11}$')

def _extract_yt_id(url: str) -> str | None:
    """Return the 11-char YouTube video ID or None."""
    try:
        x = urllib.parse.urlsplit(url.strip())
        host = (x.hostname or '').lower()
    except Exception:
        return None

    if host == 'youtu.be':
        vid = x.path.strip('/').split('/')[0].split('?')[0]
        return vid if _VID_RE.match(vid) else None

    if host not in _YT_HOSTS:
        return None

    path = x.path.rstrip('/')
    qs   = urllib.parse.parse_qs(x.query)

    # /watch?v=ID
    if path == '/watch':
        vid = (qs.get('v') or [''])[0]
        return vid if _VID_RE.match(vid) else None

    # /shorts/ID  /live/ID  /embed/ID  /v/ID  /e/ID
    for pfx in ('/shorts/', '/live/', '/embed/', '/v/', '/e/'):
        if path.startswith(pfx):
            vid = path[len(pfx):].split('/')[0].split('?')[0]
            return vid if _VID_RE.match(vid) else None

    return None

def normalize_youtube_url(u: str) -> str | None:
    """Always returns the canonical https://www.youtube.com/watch?v=ID form."""
    if not u:
        return None
    u = u.strip()
    # Bare video IDs typed directly
    if _VID_RE.match(u):
        return f'https://www.youtube.com/watch?v={u}'
    # Auto-add scheme if missing
    if '://' not in u:
        u = 'https://' + u
    vid = _extract_yt_id(u)
    return f'https://www.youtube.com/watch?v={vid}' if vid else None

def valid(u: str) -> bool:
    return normalize_youtube_url(u) is not None

# ── Error classification / help ───────────────────────────────────────────
def classify(s: str) -> str:
    s = (s or '').lower()
    if 'database is locked' in s or ('could not copy' in s and 'cookie' in s):
        return 'browser_locked'
    if 'ffmpeg' in s and any(k in s for k in
                              ('not found','not installed','could not','unable to','no such file')):
        return 'ffmpeg'
    if any(k in s for k in ('requested format is not available',
                             'requested format not available',
                             'format is not available',
                             'no video formats found',
                             'no suitable formats')):
        return 'format_unavailable'
    if any(k in s for k in ('sign in to confirm', 'not a bot', 'captcha')):
        return 'youtube_verification'
    if any(k in s for k in ('proof of origin', 'po token')):
        return 'po_token'
    if any(k in s for k in ('private video', 'login required')):
        return 'login_required'
    if any(k in s for k in ('age-restricted', 'confirm your age')):
        return 'age_restricted'
    if 'unsupported url' in s:
        return 'unsupported_url'
    if any(k in s for k in ('permission denied', 'access is denied')):
        return 'permission'
    if any(k in s for k in ('disk full', 'no space left')):
        return 'disk_full'
    if any(k in s for k in ('timed out', 'timeout')):
        return 'network_timeout'
    return 'generic'

def error_help(k: str) -> str:
    return {
        'ffmpeg': (
            'The media engine is unavailable. Click System Check, then Repair Media Engine. '
            'If Windows Security quarantined ffmpeg, allow it and retry.'
        ),
        'format_unavailable': (
            'The selected stream was unavailable or changed. '
            'Dhurta automatically tries compatible fallbacks; '
            'if all fail, re-analyze and pick a newly generated quality button.'
        ),
        'browser_locked': (
            'Browser cookies are locked. '
            'Turn off "Use browser session" and retry. Cookie-free mode is recommended.'
        ),
        'youtube_verification': (
            'YouTube requested normal verification. '
            'Open the video in YouTube, complete any verification, then re-analyze. '
            'Dhurta does not bypass verification.'
        ),
        'po_token': (
            'YouTube requires an additional token. '
            'Retry later or try another accessible video.'
        ),
        'login_required': 'This video requires account access (not available in cookie-free mode).',
        'age_restricted': 'This video requires age/account verification (not available in cookie-free mode).',
        'permission': (
            'Windows denied access to the destination folder. '
            'Choose a writable folder or check permissions.'
        ),
        'disk_full': 'Not enough free storage. Free space and retry.',
        'network_timeout': (
            'The connection timed out. '
            'Check your network, reduce concurrent fragments, then retry.'
        ),
        'unsupported_url': (
            'Supported URL forms: youtube.com/watch, /shorts, /live, /embed and youtu.be. '
            'Paste the full URL directly from the browser address bar.'
        ),
        'generic': (
            'Open "Technical log" on the job card. '
            'The first ERROR line normally identifies the failing stage. '
            'Run System Check and retry after following the suggestion.'
        ),
    }.get(k, 'Check Technical details and retry.')

# ── yt-dlp helpers ────────────────────────────────────────────────────────
def cookie_args(c: dict) -> list:
    if not c.get('use_cookies'):
        return []
    browser = c.get('browser', 'none')
    if browser in ('none', 'auto', '', None):
        return []
    return ['--cookies-from-browser', browser]

def common(c: dict) -> list:
    a = [
        '--no-playlist', '--newline', '--continue',
        '--retries',           str(max(1, int(c.get('retries', 8)))),
        '--fragment-retries',  str(max(1, int(c.get('fragment_retries', 15)))),
        '--concurrent-fragments', str(max(1, int(c.get('concurrent_fragments', 2)))),
        '--no-overwrites', '--no-cache-dir',
    ]
    a += cookie_args(c)
    if c.get('rate_limit'):
        a += ['--limit-rate', str(c['rate_limit'])]
    si  = int(c.get('sleep_interval')     or 0)
    msi = int(c.get('max_sleep_interval') or 0)
    if si:  a += ['--sleep-interval',     str(si)]
    if msi: a += ['--max-sleep-interval', str(msi)]
    ff = candidate_ffmpeg()
    if ff:
        # Pass the full executable path so yt-dlp finds non-standard names (e.g. imageio-ffmpeg)
        a += ['--ffmpeg-location', str(ff)]
    # Avoid JS runtime requirement; web player client works without deno/node
    a += ['--extractor-args', 'youtube:player_client=web,default']
    return a

# ── Format fallback ladder ────────────────────────────────────────────────
def format_ladder(q, container: str) -> list[tuple[str, str, str]]:
    """Return list of (selector, merge_container, human_label)."""
    try:
        q = int(q) if q not in (None, '', 'best') else None
    except Exception:
        q = None

    h  = f'[height={q}]'  if q else ''
    le = f'[height<={q}]' if q else ''

    if container == 'mp4':
        out = []
        if q:
            out.append((f'bv*{h}[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b{h}[ext=mp4]',
                        'mp4', f'exact {q}p MP4 (H.264+M4A)'))
        out.append(('bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]',
                    'mp4', 'best MP4 fallback'))
        if q:
            out.append((f'bv*{h}+ba/b{h}', 'mkv',
                        f'exact {q}p any codec (MKV)'))
        out.append((f'bv*{le}+ba/b{le}' if q else 'bv*+ba/best',
                    'mkv', 'best compatible resolution fallback'))
        out.append(('b[ext=mp4]/b', 'mp4', 'single-file progressive fallback'))
        out.append(('best', 'mkv', 'best available (last resort)'))
        return out

    if container == 'webm':
        out = []
        if q:
            out.append((f'bv*{h}[ext=webm]+ba[ext=webm]/b{h}[ext=webm]',
                        'webm', f'exact {q}p WebM'))
        out.append(('bv*[ext=webm]+ba[ext=webm]/b[ext=webm]',
                    'webm', 'best WebM fallback'))
        if q:
            out.append((f'bv*{h}+ba/b{h}', 'mkv',
                        f'exact {q}p any codec (MKV)'))
        out.append((f'bv*{le}+ba/b{le}' if q else 'bv*+ba/best',
                    'mkv', 'best compatible fallback'))
        out.append(('best', 'mkv', 'best available (last resort)'))
        return out

    # MKV — most permissive
    out = []
    if q:
        out.append((f'bv*{h}+ba/b{h}', 'mkv', f'exact {q}p (MKV)'))
    out.append((f'bv*{le}+ba/b{le}' if q else 'bv*+ba/best',
                'mkv', 'best compatible fallback'))
    out.append(('best', 'mkv', 'best available (last resort)'))
    return out

def audio_ladder(audio_fmt: str) -> list[tuple[str, str, str]]:
    """Format ladder for audio-only downloads."""
    fmt = (audio_fmt or 'mp3').lower()
    if fmt == 'mp3':
        return [
            ('bestaudio[ext=mp3]', 'mp3', 'best MP3'),
            ('bestaudio[ext=m4a]', 'm4a', 'best M4A (converted to MP3)'),
            ('bestaudio[ext=opus]', 'opus', 'best Opus (converted to MP3)'),
            ('bestaudio/best', 'mp3', 'best audio any format'),
        ]
    if fmt == 'm4a':
        return [
            ('bestaudio[ext=m4a]', 'm4a', 'best M4A'),
            ('bestaudio[ext=aac]', 'm4a', 'best AAC'),
            ('bestaudio/best', 'm4a', 'best audio any format'),
        ]
    if fmt == 'opus':
        return [
            ('bestaudio[ext=opus]', 'opus', 'best Opus'),
            ('bestaudio[ext=webm]', 'opus', 'best WebM audio'),
            ('bestaudio/best', 'opus', 'best audio any format'),
        ]
    # Fallback for any other format
    return [
        (f'bestaudio[ext={fmt}]', fmt, f'best {fmt}'),
        ('bestaudio/best', fmt, 'best audio any format'),
    ]

def safe_template(t: str) -> str:
    t = (t or DEFAULT['filename_template']).strip()
    t = t.replace('..', '_').replace('/', '_').replace('\\', '_')
    return t[:220] or DEFAULT['filename_template']

# ── Job helpers ───────────────────────────────────────────────────────────
def cleanup_job(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)

def log_line(j: dict, line: str) -> None:
    if line:
        j.setdefault('logs', []).append(line[-1600:])
        j['logs'] = j['logs'][-150:]

def _kill_job_proc(jid: str) -> None:
    """Terminate the subprocess for a job if it's running."""
    with LOCK:
        proc = PROCS.pop(jid, None)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass

# ── Core download thread ──────────────────────────────────────────────────
def start_job(jid: str, url: str, c: dict) -> None:
    j     = JOBS[jid]
    work  = WORK_ROOT / jid
    work.mkdir(parents=True, exist_ok=True)
    dl    = downloads_dir()
    ff    = engine_status()

    j.update(download_dir=str(dl), engine=ff, stage='preparing',
             progress=0.0, progress_source='waiting')

    mode      = j.get('mode', 'video')
    q         = j.get('quality', 'best')
    container = j.get('container', 'mp4')
    audio_fmt = c.get('audio_format', 'mp3')
    template  = safe_template(c.get('filename_template'))

    if mode == 'audio' and not ff['ready']:
        j.update(status='error', error_kind='ffmpeg',
                 message='Audio conversion needs the media engine',
                 error=ff['message'], help=error_help('ffmpeg'))
        return

    if mode == 'audio':
        raw_attempts = audio_ladder(audio_fmt)
    else:
        raw_attempts = format_ladder(q, container)

    attempts = [{'selector': x[0], 'merge': x[1], 'label': x[2]}
                for x in raw_attempts]

    last_error = ''
    j.update(status='starting', message='Preparing download…',
             error=None, error_kind=None, last_error=None,
             attempt=0, attempts=len(attempts), fallback_used=False)

    for idx, a in enumerate(attempts, 1):
        # ── Cancellation / pause check ────────────────────────────────
        if j.get('cancel_requested'):
            j.update(status='cancelled', stage='cancelled',
                     message='Cancelled by user.')
            cleanup_job(work)
            return
        if j.get('pause_requested'):
            j.update(status='paused', message='Paused. Resume to continue.',
                     pause_requested=False)
            return

        j['attempt']         = idx
        j['stage']           = 'downloading'
        j['progress']        = 0.0
        j['progress_source'] = 'yt-dlp'
        j['message']         = f'Downloading — strategy {idx}/{len(attempts)}: {a["label"]}'

        attempt_dir = work / f'attempt-{idx}'
        attempt_dir.mkdir(parents=True, exist_ok=True)

        # Build command
        args = [ytdlp_exe()] + common(c) + [
            '--paths', f'home:{dl}', f'temp:{attempt_dir}',
            '-o', template,
            # Print final file path after move — two fallback patterns
            '--print', 'after_move:__FINAL__ %(filepath)s',
            '--print', 'after_video:__FINAL_ALT__ %(filepath)s',
            # Real-time progress tokens
            '--progress-template',
            'download:__PROGRESS__|%(progress.status)s'
            '|%(progress._percent_str)s'
            '|%(progress.downloaded_bytes)s'
            '|%(progress.total_bytes)s'
            '|%(progress.speed)s'
            '|%(progress.eta)s',
            '--progress-template',
            'postprocess:__POST__|%(progress.status)s|%(progress._percent_str)s',
        ]

        if mode == 'video':
            args += ['-f', a['selector']]
            if '+' in a['selector'] and a['merge'] in ('mp4', 'mkv', 'webm'):
                args += ['--merge-output-format', a['merge']]
        else:
            args += [
                '-f', a['selector'],
                '-x', '--audio-format', audio_fmt,
                '--audio-quality', c.get('audio_quality', '192K'),
            ]

        # Optional post-processing flags
        if c.get('embed_thumbnail'):  args += ['--embed-thumbnail']
        if c.get('embed_subtitles'):  args += ['--embed-subs', '--sub-langs',
                                                c.get('subtitle_lang', 'en.*')]
        if c.get('embed_metadata'):   args += ['--embed-metadata']

        args.append(normalize_youtube_url(url) or url)

        log_file   = LOG_ROOT / f'{jid}.log'
        rc         = 1
        final_path: Path | None = None

        try:
            proc = subprocess.Popen(
                args, cwd=str(BASE), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding='utf-8', errors='replace', bufsize=1,
            )
            j['pid'] = proc.pid
            with LOCK:
                PROCS[jid] = proc

            with log_file.open('a', encoding='utf-8') as lf:
                for raw in proc.stdout:
                    line = raw.rstrip()
                    lf.write(line + '\n')
                    lf.flush()
                    log_line(j, line)

                    # ── Final file path ───────────────────────────────
                    if line.startswith('__FINAL__ '):
                        p = Path(line[len('__FINAL__ '):].strip())
                        if p.exists():
                            final_path = p
                    elif line.startswith('__FINAL_ALT__ ') and not final_path:
                        p = Path(line[len('__FINAL_ALT__ '):].strip())
                        if p.exists():
                            final_path = p

                    # ── Progress ──────────────────────────────────────
                    elif line.startswith('__PROGRESS__|'):
                        parts = line.split('|', 6)
                        if len(parts) >= 7:
                            _, status, pct, done, total, speed, eta = parts
                            try:
                                val = float(pct.replace('%', '').strip()) \
                                      if pct.strip() not in ('N/A', 'NA', 'Unknown', '') \
                                      else 0.0
                                # Download phase: 0..95%; post-processing: 95..99%
                                j['progress'] = max(0.0, min(95.0, val * 0.95))
                            except Exception:
                                pass
                            j.update(
                                downloaded=done, total=total,
                                speed=speed, eta=eta,
                                status='downloading', stage='downloading',
                                progress_source='yt-dlp',
                                message='Downloading…',
                            )

                    elif line.startswith('__POST__|'):
                        j.update(
                            status='processing', stage='processing',
                            progress=max(float(j.get('progress') or 0), 96.0),
                            progress_source='postprocessor',
                            message='Finalizing / merging media…', eta='',
                        )

                    elif any(kw in line for kw in
                             ('Merging formats', 'Merging', 'Remuxing',
                              'Embedding', 'Deleting original file', 'Fixing')):
                        j.update(
                            status='processing', stage='processing',
                            progress=max(float(j.get('progress') or 0), 96.0),
                            progress_source='postprocessor',
                            message=line[-320:],
                        )

                    if 'ERROR:' in line:
                        j['last_error'] = line[-3000:]

                    # ── Cancel / pause mid-stream ─────────────────────
                    if j.get('cancel_requested'):
                        try: proc.terminate()
                        except Exception: pass
                        j.update(status='cancelled', stage='cancelled',
                                 message='Cancelled by user.',
                                 cancel_requested=False)
                        proc.wait(timeout=10)
                        with LOCK: PROCS.pop(jid, None)
                        cleanup_job(work)
                        return

                    if j.get('pause_requested'):
                        try: proc.terminate()
                        except Exception: pass
                        j.update(status='paused', stage='paused',
                                 message='Paused. Resume to continue.',
                                 pause_requested=False)
                        proc.wait(timeout=10)
                        with LOCK: PROCS.pop(jid, None)
                        return

            rc = proc.wait()
            with LOCK: PROCS.pop(jid, None)

        except Exception as e:
            j['last_error'] = str(e)
            log_line(j, str(e))
            rc = 1
            with LOCK: PROCS.pop(jid, None)

        # ── Check success ─────────────────────────────────────────────
        # Primary: path captured by --print
        f = final_path if (final_path and final_path.exists()
                           and final_path.stat().st_size > 0) else None

        # Secondary: scan dest dir for recently written files — covers encoding
        # mismatches where final_path.exists() fails despite a successful download.
        if not f and (rc == 0 or final_path is not None):
            try:
                candidates = [
                    x for x in dl.iterdir()
                    if x.is_file()
                    and x.suffix.lower() in MEDIA_EXT
                    and not x.name.endswith('.part')
                    and x.stat().st_mtime >= j['created']
                ]
                if candidates:
                    f = max(candidates, key=lambda x: x.stat().st_mtime)
            except Exception:
                pass

        # Tertiary: match by video ID — handles --no-overwrites skip where yt-dlp
        # exits rc=0 without printing __FINAL__ because the file already existed.
        if not f and rc == 0:
            vid_id = _extract_yt_id(url) if url else None
            if vid_id:
                try:
                    for x in sorted(dl.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                        if (x.is_file() and x.suffix.lower() in MEDIA_EXT
                                and not x.name.endswith('.part')
                                and vid_id in x.name
                                and x.stat().st_size > 0):
                            f = x
                            break
                except Exception:
                    pass

        if f and f.exists() and f.stat().st_size > 0:
                j.update(
                    progress=100.0, status='done', stage='completed',
                    progress_source='verified',
                    message='Download completed and verified ✓',
                    filename=f.name, path=str(f),
                    filesize=f.stat().st_size,
                    final_format=f.suffix.lower().lstrip('.'),
                )
                if idx > 1:
                    j['fallback_used'] = True
                cleanup_job(work)
                return

        last_error = j.get('last_error') or 'Requested format is not available.'
        k = classify(last_error)

        # Non-format errors on first attempt may still be recoverable via a
        # different container/codec, so always continue through the ladder.
        j['fallback_reason'] = last_error[-1200:]

        if idx < len(attempts):
            j.update(
                status='fallback', stage='fallback',
                progress=max(float(j.get('progress') or 0), 95.0),
                message=(f'Format unavailable / incompatible. '
                         f'Trying fallback {idx+1}/{len(attempts)}: '
                         f'{attempts[idx]["label"]}…'),
                progress_source='fallback', fallback_used=True,
            )
            time.sleep(0.3)
            continue

        # All attempts exhausted
        k = classify(last_error)
        j.update(
            status='error', stage='error', error_kind=k,
            message='Download failed after all compatible fallbacks.',
            error=last_error, help=error_help(k),
            progress=max(0.0, min(99.0, float(j.get('progress') or 0))),
        )
        cleanup_job(work)

# ── Job factory ───────────────────────────────────────────────────────────
def newjob(url: str, d: dict) -> str:
    c = cfg()
    c.update(d.get('settings') or {})
    jid = uuid.uuid4().hex[:10]
    with LOCK:
        JOBS[jid] = {
            'id':     jid,
            'url':    url,
            'title':  d.get('title', url),
            'mode':   d.get('mode', 'video'),
            'quality':    d.get('quality', 'best'),
            'container':  d.get('container', 'mp4'),
            'status':     'queued',
            'progress':   0,
            'message':    'Queued',
            'created':    time.time(),
            'pause_requested':  False,
            'cancel_requested': False,
            'settings': c,
            'logs':     [],
        }
    threading.Thread(target=start_job, args=(jid, url, c), daemon=True).start()
    return jid

# ── Probe ─────────────────────────────────────────────────────────────────
def run(args: list, timeout: int | None = None):
    return subprocess.run(
        args, cwd=str(BASE), text=True, capture_output=True,
        timeout=timeout, encoding='utf-8', errors='replace',
    )

def probe(url: str, c: dict):
    url = normalize_youtube_url(url) or url
    attempts = []
    if c.get('use_cookies') and c.get('browser') not in ('none', 'auto', '', None):
        attempts.append(c['browser'])
    attempts.append('none')
    last = ''
    for b in dict.fromkeys(attempts):
        cc = c.copy()
        cc['browser']     = b
        cc['use_cookies'] = (b != 'none')
        p = run(
            [ytdlp_exe()] + common(cc) +
            ['--dump-single-json', '--skip-download', url],
            timeout=180,
        )
        out = (p.stdout or '').strip()
        if p.returncode == 0 and out:
            for line in reversed(out.splitlines()):
                if line.lstrip().startswith('{'):
                    try:
                        return json.loads(line), None, b
                    except Exception:
                        break
        last = (p.stderr or p.stdout or '')[-16000:]
        if classify(last) == 'browser_locked':
            continue
    return None, last, None

def analyze_formats(info: dict) -> list:
    heights: dict = {}
    for f in (info.get('formats') or []):
        try:
            h = int(f.get('height') or 0)
        except Exception:
            continue
        vc = f.get('vcodec')
        ac = f.get('acodec')
        if h < 1 or not vc or vc == 'none':
            continue
        item = {
            'format_id':     f.get('format_id'),
            'height':        h,
            'width':         f.get('width'),
            'fps':           f.get('fps'),
            'ext':           f.get('ext'),
            'video_codec':   vc,
            'audio_codec':   None if not ac or ac == 'none' else ac,
            'has_audio':     bool(ac and ac != 'none'),
            'filesize':      f.get('filesize') or f.get('filesize_approx'),
            'tbr':           f.get('tbr'),
            'mp4_compatible': bool(f.get('ext') == 'mp4' and
                                   str(vc).startswith('avc1')),
            'progressive':   bool(ac and ac != 'none'),
        }
        heights.setdefault(h, []).append(item)

    qs = []
    for h in sorted(heights, reverse=True):
        arr  = heights[h]
        best = max(arr, key=lambda x: (
            1 if x['mp4_compatible'] else 0,
            1 if x['has_audio'] else 0,
            x.get('filesize') or 0,
            x.get('tbr') or 0,
        ))
        qs.append({
            'height':         h,
            'width':          best.get('width'),
            'fps':            best.get('fps'),
            'ext':            best.get('ext'),
            'video_codec':    best.get('video_codec'),
            'has_audio':      best.get('has_audio'),
            'mp4_compatible': best.get('mp4_compatible'),
            'progressive':    best.get('progressive'),
            'format_ids':     [x['format_id'] for x in arr],
        })
    return qs

# ── Flask routes ──────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET', 'POST'])
def config_route():
    return jsonify(
        cfg() if request.method == 'GET'
        else save_cfg(request.get_json(silent=True) or {})
    )

@app.route('/api/health')
def health():
    try:
        y = run([ytdlp_exe(), '--version'], timeout=10).stdout.strip()
    except Exception:
        y = 'unavailable'
    e = engine_status()
    return jsonify({
        'ok': True, 'yt_dlp': y, 'engine': e,
        'python': sys.version.split()[0],
        'download_dir': str(downloads_dir()),
        'appdata': str(APPDATA),
    })

@app.route('/api/diagnostics/system')
def system_diag():
    e      = engine_status()
    checks = []
    checks.append({'name': 'Python',  'ok': True,  'detail': sys.version.split()[0]})
    y = run([ytdlp_exe(), '--version'], timeout=10)
    checks.append({'name': 'yt-dlp',  'ok': y.returncode == 0,
                   'detail': (y.stdout or y.stderr).strip()})
    checks.append({'name': 'FFmpeg',  'ok': bool(e['ffmpeg']),
                   'detail': e['ffmpeg'] or 'Not found / cannot start'})
    checks.append({'name': 'FFprobe', 'ok': bool(e['ffprobe']),
                   'detail': e['ffprobe'] or 'Not found / cannot start'})
    d = downloads_dir()
    checks.append({'name': 'Download folder', 'ok': os.access(d, os.W_OK),
                   'detail': str(d)})
    return jsonify({
        'ok':     all(x['ok'] for x in checks),
        'checks': checks,
        'engine': e,
        'tips': [
            'Use cookie-free mode first.',
            'Re-analyze before retrying a failed quality.',
            'For MP4 use a quality marked MP4-compatible when available.',
            'If FFmpeg fails, run UPDATE.bat then System Check again.',
        ],
    })

@app.route('/api/repair-engine', methods=['POST'])
def repair_engine():
    try:
        global _ffmpeg_cache
        py = BASE / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not py.exists():
            return jsonify({'ok': False,
                            'error': 'Python environment not found. Run START.bat first.'}), 400
        p = subprocess.run(
            [str(py), '-m', 'pip', 'install', '--upgrade',
             '--force-reinstall', 'imageio-ffmpeg'],
            cwd=str(BASE), text=True, capture_output=True,
            timeout=180, encoding='utf-8', errors='replace',
        )
        # Bust cache so next call re-probes
        with _ffmpeg_lock:
            _ffmpeg_cache = None
        e = engine_status()
        return jsonify({
            'ok':      p.returncode == 0 and e['available'],
            'message': e['message'],
            'engine':  e,
            'log':     (p.stdout + p.stderr)[-6000:],
        })
    except Exception as ex:
        return jsonify({'ok': False, 'error': str(ex)}), 500

@app.route('/api/analyze', methods=['POST'])
def analyze():
    d  = request.get_json(silent=True) or {}
    raw_url = (d.get('url') or '').strip()
    u  = normalize_youtube_url(raw_url)
    if not u:
        return jsonify({
            'ok': False,
            'error': (
                'Enter a valid supported YouTube URL. '
                'Supported forms: youtube.com/watch, /shorts, /live, /embed '
                'and youtu.be links.'
            ),
            'error_kind': 'unsupported_url',
            'help': error_help('unsupported_url'),
        }), 400

    c = cfg()
    c.update(d.get('settings') or {})
    b = d.get('browser') or 'none'
    if b != 'auto':
        c['browser'] = b

    info, err, used = probe(u, c)
    if not info:
        k = classify(err)
        return jsonify({'ok': False, 'error': err, 'error_kind': k,
                        'help': error_help(k)}), 400

    qs = analyze_formats(info)
    e  = engine_status()
    return jsonify({
        'ok':              True,
        'id':              info.get('id'),
        'title':           info.get('title'),
        'url':             u,
        'thumbnail':       info.get('thumbnail'),
        'duration':        info.get('duration'),
        'duration_string': info.get('duration_string'),
        'channel':         info.get('channel'),
        'uploader':        info.get('uploader'),
        'description':     info.get('description'),
        'view_count':      info.get('view_count'),
        'like_count':      info.get('like_count'),
        'upload_date':     info.get('upload_date'),
        'qualities':       qs,
        'cookie_browser_used': used,
        'download_dir':    str(downloads_dir()),
        'engine':          e,
    })

@app.route('/api/download', methods=['POST'])
def download():
    d   = request.get_json(silent=True) or {}
    u   = normalize_youtube_url((d.get('url') or '').strip())
    if not u:
        return jsonify({
            'ok': False,
            'error': 'Invalid or unsupported YouTube URL. Re-analyze the URL before downloading.',
            'error_kind': 'unsupported_url',
            'help': error_help('unsupported_url'),
        }), 400
    save_cfg(d.get('settings') or {})
    jid = newjob(u, d)
    return jsonify({
        'ok': True, 'job_id': jid,
        'download_dir': str(downloads_dir()),
        'message': 'Download queued with automatic format fallbacks.',
    })

# ── Job endpoints ─────────────────────────────────────────────────────────
@app.route('/api/jobs')
def list_jobs():
    """Return all jobs (serialisable snapshot)."""
    with LOCK:
        out = []
        for j in JOBS.values():
            safe = {k: v for k, v in j.items()
                    if k != 'settings'}   # settings is verbose, omit from list
            out.append(safe)
    return jsonify({'ok': True, 'jobs': out})

@app.route('/api/jobs/<jid>')
def job_status(jid):
    if jid not in JOBS:
        return jsonify({'ok': False}), 404
    j = {k: v for k, v in JOBS[jid].items() if k != 'settings'}
    return jsonify({'ok': True, **j})

@app.route('/api/jobs/<jid>/pause', methods=['POST'])
def pause(jid):
    if jid not in JOBS:
        return jsonify({'ok': False}), 404
    JOBS[jid]['pause_requested'] = True
    # Also send SIGTERM to the subprocess so it stops promptly
    _kill_job_proc(jid)
    return jsonify({'ok': True})

@app.route('/api/jobs/<jid>/resume', methods=['POST'])
def resume(jid):
    if jid not in JOBS:
        return jsonify({'ok': False}), 404
    j = JOBS[jid]
    if j['status'] != 'paused':
        return jsonify({'ok': False, 'error': 'Job is not paused.'}), 400
    j.update(status='queued', progress=0, pause_requested=False,
             cancel_requested=False)
    threading.Thread(target=start_job, args=(jid, j['url'], j['settings']),
                     daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/jobs/<jid>/cancel', methods=['POST'])
def cancel(jid):
    if jid not in JOBS:
        return jsonify({'ok': False}), 404
    JOBS[jid]['cancel_requested'] = True
    _kill_job_proc(jid)
    JOBS[jid].update(status='cancelled', stage='cancelled',
                     message='Cancelled by user.')
    return jsonify({'ok': True})

@app.route('/api/retry/<jid>', methods=['POST'])
def retry(jid):
    if jid not in JOBS:
        return jsonify({'ok': False}), 404
    j   = JOBS[jid]
    new = newjob(j['url'], {
        'mode':      j['mode'],
        'quality':   j['quality'],
        'container': j['container'],
        'title':     j.get('title', j['url']),
        'settings':  j['settings'],
    })
    return jsonify({'ok': True, 'job_id': new})

# ── Utility endpoints ─────────────────────────────────────────────────────
@app.route('/api/files')
def files():
    root = downloads_dir()
    out  = []
    for p in sorted(root.rglob('*'),
                    key=lambda x: x.stat().st_mtime if x.exists() else 0,
                    reverse=True):
        if p.is_file() and p.suffix.lower() in MEDIA_EXT:
            out.append({
                'name':     p.name,
                'relative': p.relative_to(root).as_posix(),
                'size':     p.stat().st_size,
                'mtime':    p.stat().st_mtime,
                'url':      '/download/' + p.relative_to(root).as_posix(),
            })
    return jsonify({'files': out, 'download_dir': str(root)})

@app.route('/api/open-download-folder', methods=['POST'])
def open_folder():
    try:
        p = downloads_dir()
        if os.name == 'nt':     os.startfile(str(p))
        elif sys.platform == 'darwin': subprocess.Popen(['open', str(p)])
        else:                   subprocess.Popen(['xdg-open', str(p)])
        return jsonify({'ok': True, 'path': str(p)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/clear-history', methods=['POST'])
def clear_history():
    with LOCK:
        JOBS.clear()
    for p in LOG_ROOT.glob('*.log'):
        try: p.unlink()
        except Exception: pass
    return jsonify({'ok': True})

@app.route('/api/diagnostics/<jid>')
def diagnostics(jid):
    if jid not in JOBS:
        return jsonify({'ok': False}), 404
    j = JOBS[jid]
    return jsonify({
        'ok': True, 'id': jid,
        'status':     j['status'],
        'error':      j.get('error'),
        'error_kind': j.get('error_kind'),
        'help':       j.get('help'),
        'logs':       j.get('logs', []),
        'fallback_used':   j.get('fallback_used'),
        'fallback_reason': j.get('fallback_reason'),
        'attempt':    j.get('attempt'),
        'attempts':   j.get('attempts'),
    })

@app.route('/api/test', methods=['POST'])
def test():
    c = cfg()
    c.update((request.get_json(silent=True) or {}).get('settings') or {})
    c['use_cookies'] = False
    c['browser']     = 'none'
    info, err, _ = probe('https://www.youtube.com/watch?v=BaW_jenozKc', c)
    if info:
        return jsonify({'ok': True,
                        'message': 'Cookie-free YouTube connection is working.'})
    k = classify(err)
    return jsonify({'ok': False, 'error': err, 'error_kind': k,
                    'help': error_help(k)}), 400

@app.route('/api/open-youtube', methods=['POST'])
def open_yt():
    u = (request.get_json(silent=True) or {}).get('url') or 'https://www.youtube.com'
    webbrowser.open(u)
    return jsonify({'ok': True})

@app.route('/download/<path:name>')
def dl(name: str):
    # Prevent path traversal
    root = downloads_dir()
    try:
        target = (root / name).resolve()
        target.relative_to(root.resolve())   # raises ValueError if outside root
    except (ValueError, OSError):
        return jsonify({'ok': False, 'error': 'Access denied.'}), 403
    return send_from_directory(root, name, as_attachment=True)

# ── Port & startup ────────────────────────────────────────────────────────
def find_free_port(start: int = 5000) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    return start

def open_browser_when_ready(url: str, timeout: int = 30) -> None:
    def _wait():
        import urllib.request
        for _ in range(timeout * 4):
            try:
                urllib.request.urlopen(f'{url}/api/health', timeout=1)
                webbrowser.open(url)
                return
            except Exception:
                time.sleep(0.25)
    threading.Thread(target=_wait, daemon=True).start()

if __name__ == '__main__':
    port = find_free_port(5000)
    base_url = f'http://127.0.0.1:{port}'
    print(f'\n{"="*55}')
    print(f'  Dhurta Media Lite v8')
    print(f'  Running at: {base_url}')
    print(f'  Downloads:  {downloads_dir()}')
    print(f'  Press Ctrl+C to stop')
    print(f'{"="*55}\n')

    open_browser_when_ready(base_url)

    # Write PID file so STOP.bat can find us
    pid_file = APPDATA / 'server.pid'
    pid_file.write_text(str(os.getpid()))

    try:
        app.run(host='127.0.0.1', port=port, threaded=True, use_reloader=False)
    finally:
        try: pid_file.unlink()
        except Exception: pass
