from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import platforms
import platform_download as fetch
import transcript_tool as tool
import knowledge_sync as sync
from douyin_download import FallbackError

SAMPLES = {
    'youtube': ('abcdefghijk', 'https://www.youtube.com/watch?v=abcdefghijk'),
    'instagram': ('ExampleClip01', 'https://www.instagram.com/reel/ExampleClip01/'),
    'tiktok': ('1234567890123456789', 'https://www.tiktok.com/@fixture_user/video/1234567890123456789'),
    'bilibili': ('BV1234567890', 'https://www.bilibili.com/video/BV1234567890/'),
}


class PlatformsTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(tempfile.gettempdir()) / 'akb-engine-platform-tests'
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=fixtures))
        self.video = platforms.identify(SAMPLES['youtube'][1])
        self.browser_video = platforms.identify(SAMPLES['instagram'][1])
        backoff = patch.object(fetch, 'sleep')
        self.backoff = backoff.start()
        self.addCleanup(backoff.stop)

    def retry_sdk(self, metadata_errors=(), media_errors=()):
        owner = self
        metadata_errors, media_errors = list(metadata_errors), list(media_errors)
        calls, options = [], []
        class Downloader:
            def __init__(self, params):
                self.params = dict(params)
                options.append(self.params)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                calls.append((download, self.params.get('outtmpl')))
                errors = media_errors if download else metadata_errors
                error = errors.pop(0) if errors else None
                if download:
                    target = Path(self.params['outtmpl'].replace('%(ext)s', 'webm'))
                    (target.parent/'retained.part').write_bytes(b'partial from this attempt')
                    target.write_bytes(b'stale first attempt' if error else b'new verified attempt')
                if error:
                    raise error
                return {'id': owner.video.raw_id, 'duration': 20}
        return SimpleNamespace(YoutubeDL=Downloader), calls, options

    def test_all_four_real_url_shapes_have_distinct_namespaced_identities(self):
        for name, (raw, url) in SAMPLES.items():
            with self.subTest(name=name):
                video = platforms.identify(url)
                self.assertEqual(video.raw_id, raw)
                self.assertEqual(video.key, name + '_' + raw)
                self.assertEqual(platforms.transcript_relative_path(video.key), Path('videos') / name / (video.key + '.md'))
                self.assertEqual(tool.extract_url('分享 ' + url), video.url)

    def test_youtube_aliases_canonicalize_without_playlist_or_tracking_state(self):
        for url in ['https://youtu.be/abcdefghijk?si=tracking',
                    'https://www.youtube.com/shorts/abcdefghijk',
                    'https://m.youtube.com/watch?v=abcdefghijk&list=tracking']:
            self.assertEqual(platforms.identify(url), self.video)

    def test_host_spoofing_and_unsafe_paths_are_not_accepted(self):
        self.assertIsNone(platforms.identify('https://youtube.com.evil.test/watch?v=abcdefghijk'))
        for url in ['http://www.youtube.com/watch?v=abcdefghijk',
                    'https://u:p@www.youtube.com/watch?v=abcdefghijk',
                    'https://www.youtube.com:444/watch?v=abcdefghijk',
                    'https://www.youtube.com/playlist?list=abc',
                    'https://www.bilibili.com/video/BV1234567890/?p=2']:
            with self.subTest(url=url), self.assertRaises(ValueError): platforms.identify(url)
        with self.assertRaises(ValueError): platforms.transcript_relative_path('youtube_../../escape')

    def test_redirect_allowlist_is_not_shared_between_platforms(self):
        self.assertTrue(platforms.media_allowed('youtube', 'https://rr1.googlevideo.com/videoplayback'))
        for value in ['http://rr1.googlevideo.com/a', 'https://rr1.googlevideo.com.evil.test/a',
                      'https://localhost/a', 'https://127.0.0.1/a', 'https://x:y@rr1.googlevideo.com/a']:
            self.assertFalse(platforms.media_allowed('youtube', value))
        self.assertFalse(platforms.media_allowed('instagram', 'https://rr1.googlevideo.com/a'))

    def test_model_failure_never_triggers_another_provider(self):
        with patch.object(fetch, 'visible_info', return_value={}), patch.object(fetch, 'download_visible') as download:
            with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
                fetch.download(self.browser_video, self.root, self.root,
                               lambda: (_ for _ in ()).throw(RuntimeError('model unavailable')))
        download.assert_not_called()

    def test_blocked_browser_does_not_import_or_run_ytdlp(self):
        fake = SimpleNamespace(YoutubeDL=lambda *_: self.fail('must not retry access denial'))
        with patch.dict(sys.modules, {'yt_dlp': fake}), patch.object(fetch, 'visible_info', side_effect=FallbackError(
                'login_required', self.browser_video.key, 'BLOCKED_PLATFORM')):
            with self.assertRaises(FallbackError) as caught: fetch.download(self.browser_video, self.root, self.root)
        self.assertEqual(caught.exception.category, 'BLOCKED_PLATFORM')

    def test_http_media_denial_is_terminal_and_attempted_once(self):
        with patch.object(fetch, 'visible_info', return_value={}), patch.object(fetch, 'download_visible', side_effect=HTTPError(
                'https://rr1.googlevideo.com/x', 403, 'Forbidden', {}, None)) as download:
            with self.assertRaises(FallbackError) as caught: fetch.download(self.browser_video, self.root, self.root)
        self.assertEqual(caught.exception.category, 'BLOCKED_PLATFORM'); self.assertEqual(download.call_count, 1)

    def test_youtube_and_tiktok_default_to_ytdlp_without_launching_a_browser(self):
        for platform in ('youtube', 'tiktok'):
            video = platforms.identify(SAMPLES[platform][1])
            workspace = self.root / platform
            workspace.mkdir()
            calls = []
            class Downloader:
                def __init__(self, options):
                    self.options = options
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, url, download):
                    calls.append((url, download))
                    if download: Path(self.options['outtmpl'].replace('%(ext)s', 'webm')).write_bytes(b'audio')
                    return {'id': video.raw_id, 'duration': 20}
            with patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=Downloader)}), patch.object(
                    fetch, 'visible_info', side_effect=AssertionError('must not launch browser')), patch.object(fetch.legacy, 'validate_media'):
                info, media = fetch.download(video, workspace, workspace)
            self.assertEqual([download for _, download in calls], [False, True])
            self.assertEqual(info['download_method'], 'yt-dlp')
            self.assertEqual(media.read_bytes(), b'audio')

    def test_ytdlp_access_denial_does_not_fall_back_to_browser(self):
        for platform in ('youtube', 'tiktok'):
            video = platforms.identify(SAMPLES[platform][1])
            class Downloader:
                def __init__(self, *args): pass
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, *args, **kwargs):
                    raise RuntimeError('HTTP Error 403: Forbidden')
            with patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=Downloader)}), patch.object(
                    fetch, 'visible_info', side_effect=AssertionError('no alternate client after denial')):
                with self.assertRaises(FallbackError) as caught:
                    fetch.download(video, self.root, self.root)
            self.assertEqual(caught.exception.category, 'BLOCKED_PLATFORM')

    def test_technical_ytdlp_failure_allows_one_browser_fallback(self):
        calls = []
        class Downloader:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, *args, **kwargs):
                calls.append('yt-dlp')
                raise RuntimeError('extractor could not parse public metadata')
        def visible(*args):
            calls.append('visible-browser')
            return {'duration': 20}
        with patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=Downloader)}), patch.object(
                fetch, 'visible_info', side_effect=visible), patch.object(fetch, 'download_visible', return_value=self.root/'media'):
            info, _ = fetch.download(self.video, self.root, self.root)
        self.assertEqual(calls, ['yt-dlp', 'visible-browser'])
        self.assertEqual(info['download_method'], 'visible-browser')

    def test_local_model_error_on_ytdlp_route_cannot_launch_browser(self):
        class Downloader:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(_self, *args, **kwargs): return {'id': self.video.raw_id}
        with patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=Downloader)}), patch.object(
                fetch, 'visible_info', side_effect=AssertionError('local error must stop')):
            with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
                fetch.download(self.video, self.root, self.root,
                               lambda: (_ for _ in ()).throw(RuntimeError('model unavailable')))

    def test_bilibili_stays_experimental_and_explicit_override_is_preserved(self):
        self.assertIn('bilibili', fetch.EXPERIMENTAL_PLATFORMS)
        self.assertEqual(fetch.route_methods('bilibili')[0], 'visible-browser')
        self.assertEqual(fetch.route_methods('instagram')[0], 'visible-browser')
        self.assertEqual(fetch.route_methods('instagram', ytdlp_first=True)[0], 'yt-dlp')

    def test_valid_media_download_uses_original_url_and_duration_validation(self):
        class Response(BytesIO):
            status = 200
            headers = {'Content-Length': '5'}
        opener = SimpleNamespace(open=lambda request, timeout: Response(b'audio'))
        info = {'media_url': 'https://rr1.googlevideo.com/audio?sig=unchanged', 'expected_duration': 12}
        with patch.object(fetch, 'build_opener', return_value=opener), patch.object(fetch.legacy, 'validate_media') as validate:
            media = fetch.download_visible(info, self.video, self.root)
        self.assertEqual(media.read_bytes(), b'audio'); validate.assert_called_once_with(media, expected_duration=12)

    def test_partial_range_cannot_be_reported_as_complete_audio(self):
        class Response(BytesIO):
            status = 206
            headers = {'Content-Range': 'bytes 0-4/100', 'Content-Length': '5'}
        info = {'media_url': 'https://rr1.googlevideo.com/audio', 'expected_duration': 12}
        with patch.object(fetch, 'build_opener', return_value=SimpleNamespace(open=lambda *_args, **_kw: Response(b'audio'))):
            with self.assertRaises(FallbackError): fetch.download_visible(info, self.video, self.root)
        self.assertFalse((self.root / 'visible-browser_media.bin').exists())

    def test_browser_owned_media_is_validated_without_python_network_request(self):
        target = self.root.resolve() / 'visible-browser_media.bin'
        target.write_bytes(b'audio')
        with patch.object(fetch, 'build_opener', side_effect=AssertionError('no external client')), patch.object(fetch.legacy, 'validate_media') as validate:
            self.assertEqual(fetch.download_visible({'media_path': str(target), 'expected_duration': 12}, self.video, self.root), target)
        validate.assert_called_once_with(target, expected_duration=12)
        with self.assertRaises(FallbackError):
            fetch.download_visible({'media_path': str(self.root/'other.bin'), 'expected_duration': 12}, self.video, self.root)

    def test_instagram_numeric_media_id_can_match_the_canonical_shortcode(self):
        video = platforms.identify(SAMPLES['instagram'][1])
        self.assertTrue(fetch.same_video(video, {'id': '123456789', 'display_id': video.raw_id}))
        self.assertFalse(fetch.same_video(video, {'id': 'other', 'display_id': 'different'}))
        self.assertFalse(fetch.same_video(video, {'_type': 'playlist', 'id': video.raw_id}))

    def test_youtube_local_runtime_options_reach_both_sdk_stages_only_for_youtube(self):
        node_options = {'js_runtimes': {'node': {'path': 'existing-node.exe'}}, 'remote_components': []}
        for platform in SAMPLES:
            video = platforms.identify(SAMPLES[platform][1])
            workspace = self.root / ('sdk-' + platform)
            workspace.mkdir()
            calls = []
            class Downloader:
                def __init__(self, options): self.options = dict(options)
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, url, download):
                    calls.append((download, self.options))
                    if download: Path(self.options['outtmpl'].replace('%(ext)s', 'webm')).write_bytes(b'audio')
                    return {'id': video.raw_id, 'duration': 20}
            with patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=Downloader)}), patch.object(
                    fetch, 'youtube_js_options', return_value=node_options) as configure, patch.object(
                    fetch, 'visible_info', side_effect=AssertionError('no browser expected')), patch.object(
                    fetch.legacy, 'validate_media'):
                fetch.download(video, workspace, workspace, ytdlp_first=True)
            self.assertEqual([download for download, _ in calls], [False, True])
            if platform == 'youtube':
                configure.assert_called_once_with()
                for _, options in calls:
                    self.assertEqual(options['js_runtimes'], node_options['js_runtimes'])
                    self.assertEqual(options['remote_components'], [])
            else:
                configure.assert_not_called()
                for _, options in calls:
                    self.assertNotIn('js_runtimes', options)
                    self.assertNotIn('remote_components', options)

    def test_youtube_prefers_explicit_node_and_reports_only_package_presence(self):
        node = self.root / 'configured-node.exe'
        node.parent.mkdir(parents=True, exist_ok=True)
        node.write_bytes(b'local runtime fixture')
        with patch.dict(fetch.os.environ, {'AKB_NODE': str(node)}), patch.object(
                fetch.shutil, 'which', side_effect=AssertionError('explicit runtime has priority')), patch.object(
                fetch.metadata, 'version', return_value='0.8.0') as version, patch.object(
                fetch.trace, 'event') as event, redirect_stdout(StringIO()) as output:
            options = fetch.youtube_js_options()
        self.assertEqual(options, {'js_runtimes': {'node': {'path': str(node)}}, 'remote_components': []})
        version.assert_called_once_with('yt-dlp-ejs')
        event.assert_any_call('local_preflight', 'package_present', method='yt-dlp',
                              source_kind='ejs_package', reason_code='youtube_ejs_package_present')
        self.assertIn('相容性由 yt-dlp 檢查', output.getvalue())

    def test_youtube_uses_existing_path_node_without_explicit_override(self):
        node = self.root / 'path-node.exe'
        node.write_bytes(b'local runtime fixture')
        with patch.object(fetch.Path, 'home', return_value=self.root), patch.object(
                fetch.shutil, 'which', return_value=str(node)) as which, patch.object(
                fetch.metadata, 'version', return_value='0.8.0'), redirect_stdout(StringIO()):
            options = fetch.youtube_js_options()
        which.assert_called_once_with('node')
        self.assertEqual(options['js_runtimes'], {'node': {'path': str(node)}})
        self.assertEqual(options['remote_components'], [])

    def test_missing_node_and_ejs_are_diagnosed_without_blocking_public_formats(self):
        with patch.object(fetch.Path, 'home', return_value=self.root), patch.object(
                fetch.shutil, 'which', return_value=None), patch.object(fetch.metadata, 'version',
                side_effect=fetch.metadata.PackageNotFoundError('yt-dlp-ejs')), patch.object(
                fetch.trace, 'event') as event, redirect_stdout(StringIO()) as output:
            options = fetch.youtube_js_options()
        self.assertEqual(options, {'remote_components': []})
        event.assert_any_call('local_preflight', 'missing', method='yt-dlp',
                              source_kind='javascript_runtime', reason_code='youtube_node_missing')
        event.assert_any_call('local_preflight', 'missing', method='yt-dlp',
                              source_kind='ejs_package', reason_code='youtube_ejs_package_missing')
        self.assertIn('找不到既有 Node.js', output.getvalue())
        self.assertIn('未安裝 yt-dlp-ejs', output.getvalue())
        self.assertIn('仍嘗試公開格式', output.getvalue())

    def test_youtube_missing_ejs_keeps_existing_node_enabled_without_remote_components(self):
        node = self.root / 'node.exe'
        node.write_bytes(b'local runtime fixture')
        with patch.object(fetch.Path, 'home', return_value=self.root), patch.object(
                fetch.shutil, 'which', return_value=str(node)), patch.object(fetch.metadata, 'version',
                side_effect=fetch.metadata.PackageNotFoundError('yt-dlp-ejs')), redirect_stdout(StringIO()):
            options = fetch.youtube_js_options()
        self.assertEqual(options, {'js_runtimes': {'node': {'path': str(node)}}, 'remote_components': []})

    def test_youtube_media_403_after_metadata_stops_without_another_provider(self):
        calls = []
        class Downloader:
            def __init__(self, options): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(_self, url, download):
                calls.append(download)
                if download: raise RuntimeError('HTTP Error 403: Forbidden')
                return {'id': self.video.raw_id, 'duration': 20}
        with patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=Downloader)}), patch.object(
                fetch, 'youtube_js_options', return_value={'remote_components': []}), patch.object(
                fetch, 'visible_info', side_effect=AssertionError('must stop after media denial')):
            with self.assertRaises(FallbackError) as caught:
                fetch.download(self.video, self.root, self.root)
        self.assertEqual(calls, [False, True])
        self.assertEqual(caught.exception.category, 'BLOCKED_PLATFORM')

    def test_youtube_timeout_then_success_keeps_two_routes_and_one_preflight(self):
        sdk, calls, options = self.retry_sdk(metadata_errors=[TimeoutError('read timed out')])
        model = Mock()
        with patch.dict(sys.modules, {'yt_dlp': sdk}), patch.object(fetch, 'youtube_js_options',
                return_value={'remote_components': []}) as configure, patch.object(fetch, 'visible_info') as browser, patch.object(
                fetch.legacy, 'validate_media'):
            info, media = fetch.download(self.video, self.root, self.root, model)
        self.assertEqual(fetch.route_methods('youtube'), ('yt-dlp', 'visible-browser'))
        self.assertEqual([download for download, _ in calls], [False, False, True])
        self.assertEqual(info['download_method'], 'yt-dlp')
        self.assertEqual(media.read_bytes(), b'new verified attempt')
        configure.assert_called_once_with()
        model.assert_called_once_with()
        browser.assert_not_called()
        self.backoff.assert_called_once_with(1)
        for params in options:
            for key in ('retries', 'fragment_retries', 'extractor_retries'):
                self.assertEqual(params[key], 0)

    def test_youtube_network_reset_retries_once_and_first_403_trace_keeps_known_identity(self):
        sdk, calls, _ = self.retry_sdk(metadata_errors=[ConnectionResetError('connection interrupted')])
        with patch.dict(sys.modules, {'yt_dlp': sdk}), patch.object(fetch, 'youtube_js_options', return_value={}), patch.object(
                fetch, 'visible_info') as browser, patch.object(fetch.legacy, 'validate_media'):
            info, _ = fetch.download(self.video, self.root, self.root)
        self.assertEqual([download for download, _ in calls], [False, False, True])
        self.assertEqual(info['download_method'], 'yt-dlp')
        self.backoff.assert_called_once_with(1)
        browser.assert_not_called()
        self.backoff.reset_mock()
        sdk, calls, _ = self.retry_sdk(metadata_errors=[HTTPError(self.video.url, 403, 'Denied', {}, None)])
        log = self.root/'known-403.jsonl'
        with patch.dict(sys.modules, {'yt_dlp': sdk}), patch.object(fetch, 'youtube_js_options', return_value={}), patch.object(
                fetch, 'visible_info') as browser, fetch.trace.Trace(log), self.assertRaises(FallbackError):
            fetch.download(self.video, self.root, self.root)
        self.assertEqual(json.loads(log.read_text(encoding='utf-8'))['video_id'], self.video.key)
        self.assertEqual(len(calls), 1)
        self.backoff.assert_not_called()
        browser.assert_not_called()

    def test_youtube_media_retry_retains_partial_and_uses_only_new_attempt_files(self):
        sdk, calls, _ = self.retry_sdk(media_errors=[HTTPError(self.video.url, 503, 'Temporary', {}, None)])
        model = Mock()
        with patch.dict(sys.modules, {'yt_dlp': sdk}), patch.object(fetch, 'youtube_js_options', return_value={}), patch.object(
                fetch, 'visible_info') as browser, patch.object(fetch.legacy, 'validate_media'):
            _, media = fetch.download(self.video, self.root, self.root, model)
        templates = [Path(template.replace('%(ext)s', 'webm')) for download, template in calls if download]
        self.assertEqual(len(templates), 2)
        self.assertNotEqual(templates[0].parent, templates[1].parent)
        self.assertEqual(templates[0].read_bytes(), b'stale first attempt')
        self.assertEqual((templates[0].parent/'retained.part').read_bytes(), b'partial from this attempt')
        self.assertEqual(media, templates[1])
        self.assertEqual(media.read_bytes(), b'new verified attempt')
        model.assert_called_once_with()
        browser.assert_not_called()
        self.backoff.assert_called_once_with(1)

    def test_youtube_two_timeouts_then_existing_browser_fallback(self):
        sdk, calls, _ = self.retry_sdk(metadata_errors=[TimeoutError('read timed out')] * 2)
        model = Mock()
        with patch.dict(sys.modules, {'yt_dlp': sdk}), patch.object(fetch, 'youtube_js_options', return_value={}), patch.object(
                fetch, 'visible_info', return_value={'duration': 20}) as browser, patch.object(
                fetch, 'download_visible', return_value=self.root/'mock-media'):
            info, _ = fetch.download(self.video, self.root, self.root, model)
        self.assertEqual([download for download, _ in calls], [False, False])
        browser.assert_called_once()
        self.assertEqual(info['download_method'], 'visible-browser')
        self.backoff.assert_called_once_with(1)
        model.assert_called_once_with()

    def test_youtube_all_recoverable_routes_exhaust_at_two_attempts_with_no_asr(self):
        sdk, calls, _ = self.retry_sdk(metadata_errors=[TimeoutError('read timed out')] * 2)
        model = Mock()
        log = self.root/'bounded.jsonl'
        with patch.dict(sys.modules, {'yt_dlp': sdk}), patch.object(fetch, 'youtube_js_options', return_value={}), patch.object(
                fetch, 'visible_info', side_effect=FallbackError('complete_original_audio_not_observed', self.video.key,
                    'NO_SUPPORTED_MEDIA')) as browser, fetch.trace.Trace(log), self.assertRaises(FallbackError):
            fetch.download(self.video, self.root, self.root, model)
        self.assertEqual(len(calls), 2)
        self.assertEqual(browser.call_count, 2)
        self.assertEqual(self.backoff.call_count, 2)
        model.assert_not_called()
        data = json.loads(log.read_text(encoding='utf-8'))
        attempts = [event for event in data['events'] if event['stage'] == 'route_attempt']
        self.assertEqual([(event['method'], event['attempt'], event['max_attempts']) for event in attempts],
                         [('yt-dlp', 1, 2), ('yt-dlp', 2, 2), ('visible-browser', 1, 2), ('visible-browser', 2, 2)])
        self.assertEqual(data['stages']['transcription'], 'not_attempted')
        self.assertNotIn(self.video.url, log.read_text(encoding='utf-8'))

    def test_youtube_terminal_source_and_local_errors_never_retry_or_switch_provider(self):
        errors = [HTTPError(self.video.url, code, 'Denied', {}, None) for code in (401, 403, 412, 429, 451)]
        errors += [RuntimeError(reason) for reason in ('login required', 'captcha', 'private video', 'DRM',
                                                     'not available in your country')]
        errors += [FallbackError('target mismatch', self.video.key, 'INVALID_TARGET'),
                   FallbackError('protected', self.video.key, 'PROTECTED_MEDIA'),
                   ModuleNotFoundError('missing dependency'), PermissionError('disk access')]
        for error in errors:
            with self.subTest(error=repr(error)):
                self.backoff.reset_mock()
                sdk, calls, _ = self.retry_sdk(metadata_errors=[error])
                with patch.dict(sys.modules, {'yt_dlp': sdk}), patch.object(fetch, 'youtube_js_options', return_value={}), patch.object(
                        fetch, 'visible_info') as browser, self.assertRaises(Exception):
                    fetch.download(self.video, self.root, self.root)
                self.assertEqual(len(calls), 1)
                browser.assert_not_called()
                self.backoff.assert_not_called()

    def test_namespaced_existing_transcript_skips_browser_and_model(self):
        target = self.root / platforms.transcript_relative_path(self.video.key)
        target.parent.mkdir(parents=True)
        target.write_text(f'- 影片 ID：{self.video.key}\n## 完整逐字稿\n- 原始分享連結：{self.video.url}\n- 辨識模型：x\n- 語言：en\n[00:00:00.000 → 00:00:01.000] text\n', encoding='utf-8')
        with patch.object(tool, 'KNOWLEDGE_TRANSCRIPTS', self.root/'videos/douyin'), patch.object(fetch, 'download') as download:
            info, media = tool.download_for_transcription(self.video.url, self.root)
        self.assertEqual(info['_skip_status'], 'skipped_existing'); self.assertIsNone(media); download.assert_not_called()

    def test_output_metadata_and_collection_preserve_platform_identity(self):
        p = tool.write_outputs(self.root, {'id': self.video.key, 'title': 'Example', 'platform': 'youtube',
            'source_url': self.video.url}, [SimpleNamespace(start=0, end=1, text='original words')], 'en')
        self.assertIn('- 來源平台：youtube', p.read_text(encoding='utf-8'))
        self.assertEqual(sync.collect_sources(self.root), {self.video.key: p})
        p.write_text(p.read_text(encoding='utf-8').replace(self.video.url, SAMPLES['instagram'][1]), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '來源平台'): sync.collect_sources(self.root)

    def test_empty_speech_stays_complete_with_and_without_platform_metadata(self):
        for metadata in [dict(id='12345', title='Silent legacy', source_url='https://www.douyin.com/video/12345'),
                         dict(id=self.video.key, platform='youtube', title='Silent platform', source_url=self.video.url)]:
            p = tool.write_outputs(self.root, metadata, [], 'en')
            self.assertIn('（未辨識到語音）', p.read_text(encoding='utf-8'))
            self.assertTrue(tool.is_complete(p))

    def test_legacy_sync_never_pushes_new_platform_outputs(self):
        tool.write_outputs(self.root, dict(id=self.video.key, platform='youtube', title='Example',
            source_url=self.video.url), [SimpleNamespace(start=0, end=1, text='words')], 'en')
        repo = self.root / 'repo'
        (repo / '.git').mkdir(parents=True)
        result = sync.sync_outputs(self.root, repo)
        self.assertFalse(result.ok)
        self.assertIn('停用遠端同步', result.message)
        self.assertFalse(result.pushed)



if __name__ == '__main__': unittest.main()
