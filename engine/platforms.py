"""Explicit single-video identities; never accepts arbitrary websites or media hosts."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

RULES = json.loads(Path(__file__).with_name('platform_rules.json').read_text(encoding='utf-8'))
ID_PATTERNS = {'youtube': r'[A-Za-z0-9_-]{11}', 'instagram': r'[A-Za-z0-9_-]{5,40}',
               'tiktok': r'\d{10,25}', 'bilibili': r'BV[A-Za-z0-9]{10}'}


@dataclass(frozen=True)
class Video:
    platform: str
    raw_id: str
    url: str

    @property
    def key(self):
        return f'{self.platform}_{self.raw_id}'


def identify(value: str) -> Video | None:
    """None means another provider; supported hosts with invalid paths are rejected."""
    try:
        u = urlsplit(value)
        host = (u.hostname or '').lower()
        if host not in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be',
                        'instagram.com', 'www.instagram.com', 'tiktok.com', 'www.tiktok.com',
                        'm.tiktok.com', 'bilibili.com', 'www.bilibili.com', 'm.bilibili.com'}:
            return None
        if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
            raise ValueError('影片網址必須使用 HTTPS，且不能包含登入資訊或自訂連接埠')
        if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}:
            if host == 'youtu.be':
                raw = u.path.strip('/')
            elif u.path == '/watch':
                ids = parse_qs(u.query).get('v', [])
                raw = ids[0] if len(ids) == 1 else ''
            else:
                m = re.fullmatch(r'/(?:shorts|embed)/([^/]+)/?', u.path)
                raw = m[1] if m else ''
            platform, canonical = 'youtube', f'https://www.youtube.com/watch?v={raw}'
        elif host.endswith('instagram.com'):
            m = re.fullmatch(r'/(reel|p|tv)/([A-Za-z0-9_-]+)/?', u.path)
            raw = m[2] if m else ''
            platform, canonical = 'instagram', f'https://www.instagram.com/{m[1] if m else "reel"}/{raw}/'
        elif host.endswith('tiktok.com'):
            m = re.fullmatch(r'/@([A-Za-z0-9_.-]+)/video/(\d+)/?', u.path)
            raw = m[2] if m else ''
            platform, canonical = 'tiktok', f'https://www.tiktok.com/@{m[1] if m else "unknown"}/video/{raw}'
        else:
            m = re.fullmatch(r'/video/(BV[A-Za-z0-9]{10})/?', u.path)
            raw = m[1] if m else ''
            pages = parse_qs(u.query).get('p', ['1'])
            if pages != ['1']:
                raise ValueError('目前僅支援 Bilibili 第一分 P；未將不同分 P 當作同一段音訊')
            platform, canonical = 'bilibili', f'https://www.bilibili.com/video/{raw}/'
        if not re.fullmatch(ID_PATTERNS[platform], raw):
            raise ValueError('請提供支援平台的完整單一影片網址；不接受首頁、清單或未解析的短網址')
        return Video(platform, raw, canonical)
    except (TypeError, AttributeError) as error:
        raise ValueError('影片網址格式不正確') from error


def platform_for_key(key: str) -> str:
    prefix, _, raw = key.partition('_')
    if prefix in RULES:
        if not re.fullmatch(ID_PATTERNS[prefix], raw):
            raise ValueError('平台影片 ID 格式不正確')
        return prefix
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', key):
        raise ValueError('影片 ID 格式不正確')
    return 'douyin'


def transcript_relative_path(key: str) -> Path:
    return Path('videos') / platform_for_key(key) / f'{key}.md'


def media_allowed(platform: str, value: str) -> bool:
    try:
        u = urlsplit(value)
        rule = RULES[platform]
        return bool(u.scheme == 'https' and not u.username and not u.password and u.port in (None, 443)
                    and (u.hostname in rule['media_hosts'] or any(
                        u.hostname == h or (u.hostname or '').endswith('.' + h)
                        for h in rule['media_suffixes'])))
    except (ValueError, KeyError, TypeError):
        return False
