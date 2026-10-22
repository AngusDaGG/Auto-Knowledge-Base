"""Per-platform download routes; anonymous, bounded and local."""
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

import download_diagnostics as trace
import douyin_download as legacy
from platforms import Video, identify, media_allowed

ROOT = Path(__file__).resolve().parent
TERMINAL = {'BLOCKED_PLATFORM', 'INVALID_TARGET', 'PROTECTED_MEDIA'}
DEFAULT_ROUTES = {
    'youtube': ('yt-dlp', 'visible-browser'),
    'tiktok': ('yt-dlp', 'visible-browser'),
    'instagram': ('visible-browser', 'yt-dlp'),
    'bilibili': ('visible-browser', 'yt-dlp'),
}
EXPERIMENTAL_PLATFORMS = frozenset({'bilibili'})


def route_methods(platform, ytdlp_first=False):
    # An explicit compatibility override does not permit retrying access denial.
    default = DEFAULT_ROUTES[platform]
    return ('yt-dlp', 'visible-browser') if ytdlp_first else default


def youtube_js_options():
    # Use only installed local components; yt-dlp checks their compatibility.
    discovered = os.environ.get('AKB_NODE') or shutil.which('node')
    node = Path(discovered) if discovered else None
    options = {'remote_components': []}
    if node is not None and node.is_file():
        options['js_runtimes'] = {'node': {'path': str(node)}}
        trace.event('local_preflight', 'observed', method='yt-dlp',
                    source_kind='javascript_runtime', reason_code='youtube_node_selected')
    else:
        trace.event('local_preflight', 'missing', method='yt-dlp',
                    source_kind='javascript_runtime', reason_code='youtube_node_missing')
        print('YouTube：找不到既有 Node.js；沿用 yt-dlp 預設 runtime 設定，不自動安裝。', flush=True)
    try:
        ejs_version = metadata.version('yt-dlp-ejs')
    except metadata.PackageNotFoundError:
        trace.event('local_preflight', 'missing', method='yt-dlp',
                    source_kind='ejs_package', reason_code='youtube_ejs_package_missing')
        print('YouTube：未安裝 yt-dlp-ejs；部分格式可能無法取得，仍嘗試公開格式，不下載額外元件。', flush=True)
    else:
        trace.event('local_preflight', 'package_present', method='yt-dlp',
                    source_kind='ejs_package', reason_code='youtube_ejs_package_present')
        print(f'YouTube：已找到 yt-dlp-ejs {ejs_version}；相容性由 yt-dlp 檢查。', flush=True)
    return options


def same_video(video, info):
    if not isinstance(info, dict) or info.get('_type') in ('playlist', 'multi_video'):
        return False
    if video.raw_id in (str(info.get('id')), str(info.get('display_id'))):
        return True
    try:
        target = identify(info.get('webpage_url', ''))
        return bool(target and target.key == video.key)
    except ValueError:
        return False


def visible_info(video: Video, workspace: Path, temporary: Path | None = None):
    node, module = legacy.browser_runtime(video.key)
    profile = workspace / 'state' / video.platform / 'visible-browser-profile'
    profile.parent.mkdir(parents=True, exist_ok=True)
    output = temporary.resolve() / 'visible-browser_media.bin' if temporary is not None else None
    try:
        result = subprocess.run([str(node), str(ROOT / 'platform_browser_probe.cjs'), str(module),
                                 video.platform, video.url, video.raw_id, str(profile.resolve()),
                                 *([str(output)] if output else [])],
                                capture_output=True, text=True, encoding='utf-8', timeout=105)
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise legacy.FallbackError('獨立瀏覽器執行失敗：' + type(error).__name__, video.key,
                                   'LOCAL_BROWSER_ERROR', 'page_load') from error
    diagnostic = payload.get('diagnostics') or {}
    trace.event('page_load', 'success' if diagnostic.get('navigation_status') == 200 else 'failed',
                method='visible-browser', http_status=diagnostic.get('navigation_status'),
                browser_closed=diagnostic.get('browser_closed'), reason_code=payload.get('reason'))
    trace.event('metadata', 'observed', method='visible-browser', source_kind=diagnostic.get('location_kind'))
    if diagnostic.get('failure_phase'):
        trace.event('route', 'observed', method='visible-browser', source_kind=diagnostic.get('failure_phase'),
                    error_type=diagnostic.get('error_type'), reason_code=diagnostic.get('network_error'))
    if diagnostic.get('capture_started') or diagnostic.get('media_status'):
        trace.event('media_download', 'success' if payload.get('ok') else 'failed', method='visible-browser',
                    http_status=diagnostic.get('media_status'), byte_count=diagnostic.get('byte_count'),
                    source_kind=diagnostic.get('capture_kind'), reason_code=payload.get('reason'))
    if not payload.get('ok') or result.returncode:
        error = legacy.FallbackError('獨立瀏覽器：' + str(payload.get('reason', 'no_media')),
                                     video.key, payload.get('category', 'UNKNOWN'),
                                     'media_download' if diagnostic.get('capture_started') else 'metadata')
        error.diagnostics = diagnostic
        raise error
    info = payload.get('info') or {}
    if (info.get('raw_id') != video.raw_id or info.get('platform') != video.platform
            or not media_allowed(video.platform, info.get('media_url', ''))):
        raise legacy.FallbackError('播放器來源與目標不符', video.key, 'INVALID_TARGET', 'metadata')
    duration = info.get('expected_duration')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise legacy.FallbackError('沒有完整且有限的影片長度', video.key, 'NO_SUPPORTED_MEDIA', 'metadata')
    if output is not None and (info.get('media_path') != str(output) or not output.is_file()):
        raise legacy.FallbackError('瀏覽器沒有提供本次專用的媒體檔案', video.key, 'LOCAL_BROWSER_ERROR', 'media_download')
    info.update(id=video.key, download_method='visible-browser')
    trace.event('metadata', 'success', method='visible-browser')
    return info


def download_visible(info, video: Video, temporary: Path):
    if 'media_path' in info:
        target = temporary.resolve() / 'visible-browser_media.bin'
        if info['media_path'] != str(target) or not target.is_file() or not 0 < target.stat().st_size <= legacy.MAX_MEDIA_BYTES:
            raise legacy.FallbackError('瀏覽器媒體路徑或大小不符', video.key, 'LOCAL_BROWSER_ERROR', 'media_download')
        legacy.validate_media(target, expected_duration=info['expected_duration'])
        return target
    url = info['media_url']
    if not media_allowed(video.platform, url):
        raise legacy.FallbackError('不支援的媒體來源', video.key, 'NO_SUPPORTED_MEDIA', 'media_url')
    trace.event('media_url', 'success', method='visible-browser', candidate_count=1)
    opener = build_opener(legacy.CheckedRedirect(lambda target: media_allowed(video.platform, target)))
    target = temporary / 'visible-browser_media.bin'
    try:
        with trace.stage('media_download', method='visible-browser') as metrics:
            request = Request(url, headers={'User-Agent': info.get('user_agent') or 'Mozilla/5.0',
                                            'Referer': video.url})
            with opener.open(request, timeout=20) as response:
                metrics['http_status'] = response.status
                if response.status not in (200, 206):
                    raise legacy.FallbackError('媒體回傳非成功狀態', video.key, 'NO_SUPPORTED_MEDIA', 'media_download')
                if response.status == 206:
                    match = re.fullmatch(r'bytes 0-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match or int(match[1]) + 1 != int(match[2]):
                        raise legacy.FallbackError('播放器只提供部分片段，未拼接為完整音訊', video.key,
                                                   'NO_SUPPORTED_MEDIA', 'media_download')
                length = response.headers.get('Content-Length')
                declared = int(length) if length else None
                if declared is not None and declared > legacy.MAX_MEDIA_BYTES:
                    raise legacy.FallbackError('媒體超過 512 MiB', video.key, 'MEDIA_LIMIT', 'media_download')
                size = 0
                with target.open('xb') as handle:
                    while chunk := response.read(256 * 1024):
                        size += len(chunk)
                        if size > legacy.MAX_MEDIA_BYTES:
                            raise legacy.FallbackError('媒體超過 512 MiB', video.key, 'MEDIA_LIMIT', 'media_download')
                        handle.write(chunk)
                if not size or declared is not None and size != declared:
                    raise legacy.FallbackError('媒體下載未完成', video.key, 'NO_SUPPORTED_MEDIA', 'media_download')
                metrics['byte_count'] = size
        legacy.validate_media(target, expected_duration=info['expected_duration'])
        return target
    except Exception:
        if target.exists():
            target.unlink()
        raise


def download(video: Video, temporary: Path, workspace: Path, before_download=None, ytdlp_first=False):
    trace.set_video_id(video.key)
    methods = route_methods(video.platform, ytdlp_first)
    if video.platform in EXPERIMENTAL_PLATFORMS:
        print('Bilibili：實驗支援，尚未完成真實轉錄驗收。', flush=True)
    last_error = None
    model_ready = False
    options = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'retries': 0,
               'fragment_retries': 0, 'extractor_retries': 0, 'socket_timeout': 20,
               'format': 'bestaudio/best', 'max_filesize': legacy.MAX_MEDIA_BYTES,
               'logger': trace.SafeYtdlpLogger(), 'cachedir': False}
    if os.environ.get('AKB_FFMPEG'):
        options['ffmpeg_location'] = os.environ['AKB_FFMPEG']
    if video.platform == 'youtube':
        options.update(youtube_js_options())
    max_attempts = 2 if video.platform == 'youtube' else 1
    for method in methods:
        for attempt in range(1, max_attempts + 1):
            attempt_temporary = (Path(tempfile.mkdtemp(prefix=f'{method}-{attempt}-', dir=temporary))
                                 if video.platform == 'youtube' else temporary)
            trace.event('route_attempt', 'started', method=method, attempt=attempt, max_attempts=max_attempts)
            print(f'{video.platform}：{method}…', flush=True)
            try:
                if method == 'visible-browser':
                    info = visible_info(video, workspace, attempt_temporary)
                else:
                    import yt_dlp
                    with trace.stage('metadata', method=method), yt_dlp.YoutubeDL(options) as probe:
                        info = probe.extract_info(video.url, download=False)
                    if not same_video(video, info):
                        raise legacy.FallbackError('備案回傳不同影片 ID', video.key, 'INVALID_TARGET', 'metadata')
                    if info.get('is_live') or info.get('has_drm'):
                        raise legacy.FallbackError('不處理直播或受保護媒體', video.key, 'PROTECTED_MEDIA', 'metadata')
                if before_download and not model_ready:
                    # Local model errors are not site/provider failures.
                    try:
                        before_download()
                    except Exception as error:
                        error.video_id = video.key
                        raise LocalPreflightError(error) from error
                    model_ready = True
                if method == 'visible-browser':
                    media = download_visible(info, video, attempt_temporary)
                else:
                    import yt_dlp
                    params = dict(options, outtmpl=str(attempt_temporary / 'fallback.%(ext)s'))
                    with trace.stage('media_download', method=method), yt_dlp.YoutubeDL(params) as downloader:
                        downloaded = downloader.extract_info(video.url, download=True)
                    if not same_video(video, downloaded):
                        raise legacy.FallbackError('備案下載前後 ID 不同', video.key, 'INVALID_TARGET', 'media_download')
                    candidates = [p for p in attempt_temporary.glob('fallback.*') if p.suffix not in ('.part', '.ytdl')]
                    if len(candidates) != 1 or not 0 < candidates[0].stat().st_size <= legacy.MAX_MEDIA_BYTES:
                        raise legacy.FallbackError('備案沒有單一完整媒體', video.key, 'NO_SUPPORTED_MEDIA', 'media_download')
                    media = candidates[0]
                    legacy.validate_media(media, expected_duration=info.get('duration'))
                    info = downloaded
                info.update(id=video.key, platform=video.platform, raw_id=video.raw_id, download_method=method)
                print(f'{method} 媒體已下載並驗證', flush=True)
                trace.event('route_attempt', 'success', method=method, attempt=attempt, max_attempts=max_attempts)
                return info, media
            except LocalPreflightError as error:
                raise error.original
            except Exception as error:
                category = trace.category(error)
                reason = trace.retry_reason(error)
                trace.event('route', 'failed', method=method, error_type=type(error).__name__,
                            attempt=attempt, max_attempts=max_attempts, reason_code=reason or 'non_retryable')
                if category in TERMINAL:
                    raise legacy.FallbackError(trace.safe_reason(error), video.key, category,
                                               getattr(error, 'stage', 'metadata')) from error
                if trace.local_failure(error):
                    raise
                last_error = error
                if reason and attempt < max_attempts:
                    trace.event('route_retry', 'scheduled', method=method, attempt=attempt,
                                max_attempts=max_attempts, reason_code=reason)
                    sleep(1)
                    continue
                break
    raise legacy.FallbackError(trace.safe_reason(last_error), video.key,
                               trace.category(last_error), getattr(last_error, 'stage', 'metadata')) from last_error


class LocalPreflightError(Exception):
    def __init__(self, original):
        self.original = original
