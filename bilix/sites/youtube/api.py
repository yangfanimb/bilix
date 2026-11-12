"""
2026-11 重写：网页 ytInitialPlayerResponse 的 adaptiveFormats 已无直链（SABR改造，需PO token），
改用 innertube API：
  1. ANDROID 客户端：adaptiveFormats 有直链时直接用（部分内容仍可用）
  2. IOS 客户端：返回 hlsManifestUrl，解析 master 清单取视频/音频分轨清单（免PO token获取高清）
  3. 兜底：muxed itag 18 (360p mp4) 单文件
限制：clientVersion 有硬校验，未来失效时需跟随 yt-dlp 更新版本号；
      数据中心IP可能命中"Sign in to confirm you're not a bot"（需登录cookie或住宅IP）。
详见 docs/dev/site-support-status.md 平台方案记录。
"""
import re
import json
from typing import Optional
from pydantic import BaseModel
import httpx
from bilix.download.utils import req_retry, raise_api_error
from bilix.utils import legal_title
from bilix.exception import APIUnsupportedError, APIResourceError

INNERTUBE_URL = 'https://www.youtube.com/youtubei/v1/player?prettyPrint=false'
# clientVersion 有硬校验：跟随 yt-dlp 更新
CLIENTS = {
    'android': {
        'context': {'client': {'clientName': 'ANDROID', 'clientVersion': '21.26.364',
                               'androidSdkVersion': 30, 'osName': 'Android', 'osVersion': '11',
                               'hl': 'en', 'gl': 'US'}},
        'user-agent': 'com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip',
    },
    'ios': {
        'context': {'client': {'clientName': 'IOS', 'clientVersion': '21.26.4',
                               'deviceModel': 'iPhone16,2', 'hl': 'en', 'gl': 'US'}},
        'user-agent': 'com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;) gzip',
    },
}

dft_client_settings = {
    'headers': {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36',
        'referer': 'https://www.youtube.com/'
    },
}


class VideoInfo(BaseModel):
    title: str
    video_url: str
    audio_url: Optional[str] = None  # None 表示视频流已含音轨（muxed），无需二次下载与合并
    hls: bool = False  # video_url/audio_url 是否为 HLS 清单（需用m3u8管线下载）


def _parse_video_id(url: str) -> str:
    for pattern in (r'[?&]v=([\w-]+)', r'youtu\.be/([\w-]+)', r'/shorts/([\w-]+)', r'/live/([\w-]+)', r'/embed/([\w-]+)'):
        if m := re.search(pattern, url):
            return m.groups()[0]
    raise APIUnsupportedError(f'无法从url解析视频id', url)


async def _player(client: httpx.AsyncClient, client_type: str, video_id: str) -> dict:
    conf = CLIENTS[client_type]
    body = {'context': conf['context'], 'videoId': video_id,
            'contentCheckOk': True, 'racyCheckOk': True}
    res = await req_retry(client, INNERTUBE_URL, method='POST', json=body,
                          headers={'user-agent': conf['user-agent'], 'content-type': 'application/json'})
    data = res.json()
    status = (data.get('playabilityStatus') or {}).get('status', 'UNKNOWN')
    if status not in ('OK', 'LIVE_STREAM_OFFLINE'):
        reason = (data.get('playabilityStatus') or {}).get('reason', status)
        if status == 'LOGIN_REQUIRED':
            raise APIResourceError(f'YouTube风控({reason})：需登录cookie（-fb导入浏览器cookie）或更换网络', video_id)
        raise APIResourceError(f'视频不可播放: {reason}', video_id)
    return data


def _pick_adaptive(streams: list, mime_prefix: str, prefer_itags=()) -> dict | None:
    """从带直链的流中按 prefer_itags 优先、否则按 bitrate 最高选取"""
    cands = [f for f in streams if f.get('url') and f.get('mimeType', '').startswith(mime_prefix)]
    if not cands:
        return None
    for itag in prefer_itags:
        for f in cands:
            if f.get('itag') == itag:
                return f
    return max(cands, key=lambda f: f.get('bitrate', 0))


def _parse_hls_manifest(manifest_text: str) -> tuple[str, str | None]:
    """从 master 清单解析出（最高画质视频清单URL, 音频清单URL）"""
    videos = _parse_hls_variants(manifest_text)
    if not videos:
        raise APIUnsupportedError('HLS master清单中未找到视频流', manifest_text[:100])
    video_url = max(videos, key=lambda v: v[1])[0]
    # 音频：EXT-X-MEDIA 行中 TYPE=AUDIO 的 URI（优先 DEFAULT=YES；属性顺序不定，逐行判断）
    audio_url = None
    for m in re.finditer(r'#EXT-X-MEDIA:[^\n]*', manifest_text):
        seg = m.group(0)
        if 'TYPE=AUDIO' not in seg:
            continue
        uri = re.search(r'URI="([^"]+)"', seg)
        if uri and (audio_url is None or 'DEFAULT=YES' in seg.upper()):
            audio_url = uri.group(1)
    return video_url, audio_url


def _parse_hls_variants(manifest_text: str) -> list:
    """解析 master 清单的视频分轨: [(清单URL, 高度, BANDWIDTH)]，按高度降序"""
    variants = []
    for m in re.finditer(r'#EXT-X-STREAM-INF:[^\n]*\n([^\n#][^\n]*)', manifest_text):
        attrs = m.group(0)
        res = re.search(r'RESOLUTION=(\d+)x(\d+)', attrs)
        bw = int(re.search(r'BANDWIDTH=(\d+)', attrs).group(1)) if re.search(r'BANDWIDTH=(\d+)', attrs) else 0
        height = int(res.group(2)) if res else 0
        variants.append((m.group(1).strip(), height, bw))
    return sorted(variants, key=lambda v: -v[1])


def _pick_hls_by_quality(variants: list, quality) -> str:
    """按画质参数选择视频分轨: 0/None=最高；'1080p'字符串=高度≤目标的最大者，无则取最接近的"""
    if not variants:
        raise APIUnsupportedError('HLS master清单中未找到视频流', '')
    if not quality:
        return variants[0][0]
    if isinstance(quality, str):
        m = re.match(r'(\d+)\s*p', quality.strip().lower())
        target = int(m.group(1)) if m else None
    else:  # 数字按B站语义是相对档位，youtube简化为: 0最高，n=第n高
        if isinstance(quality, int) and 0 < quality <= len(variants):
            return variants[quality - 1][0]
        return variants[0][0]
    if target is None:
        return variants[0][0]
    fit = [v for v in variants if v[1] <= target]
    return (fit[0] if fit else variants[-1])[0]  # ≤目标取最大，否则取最小的向上兜底


def _get_caption_tracks(player_data: dict) -> list:
    """从player response解析字幕轨: [(baseUrl, 语言名, 语言代码)]"""
    tracks = []
    renderer = ((data_captions := player_data.get('captions')) or {}).get('playerCaptionsTracklistRenderer') or {}
    for t in renderer.get('captionTracks') or []:
        name = ''.join(r.get('text', '') for r in (t.get('name') or {}).get('runs') or []) or t.get('languageCode', '')
        tracks.append((t['baseUrl'], name, t.get('languageCode', '')))
    return tracks


def convert_subtitle(content: bytes) -> bytes:
    """YouTube字幕转SRT：自动识别 timedtext XML（默认返回格式）与 json3 两种格式"""
    text_head = content[:200].lstrip()
    if text_head.startswith(b'<?xml') or b'<timedtext' in text_head:
        import xml.etree.ElementTree as ET
        out, idx = [], 0
        root = ET.fromstring(content)

        def fmt(ms: int) -> str:
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms = divmod(ms, 1000)
            return f'{h:02}:{m:02}:{s:02},{ms:03}'

        for p in root.iter('p'):
            text = ''.join(p.itertext()).replace('\n', ' ').strip()
            if not text:
                continue
            idx += 1
            start = int(p.get('t', 0))
            dur = int(p.get('d', 2000))
            out.append(f'{idx}\n{fmt(start)} --> {fmt(start + dur)}\n{text}\n')
        return '\n'.join(out).encode()
    # json3 格式
    data = json.loads(content)
    out, idx = [], 0

    def fmt(ms: int) -> str:
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'

    for ev in data.get('events') or []:
        text = ''.join(seg.get('utf8', '') for seg in ev.get('segs') or []).replace('\n', ' ').strip()
        if not text:
            continue
        idx += 1
        start = ev.get('tStartMs', 0)
        dur = ev.get('dDurationMs', 2000)
        out.append(f'{idx}\n{fmt(start)} --> {fmt(start + dur)}\n{text}\n')
    return '\n'.join(out).encode()


@raise_api_error
async def get_video_info(client: httpx.AsyncClient, url: str, quality=0):
    """获取YouTube视频信息

    :param quality: 画质选择，0=最高；'1080p'等字符串=高度≤目标的最大者（HLS路径生效）
    """
    video_id = _parse_video_id(url)
    data = await _player(client, 'android', video_id)
    title = legal_title(data['videoDetails']['title'])
    sd = data.get('streamingData') or {}

    # 1. adaptive 直链（未被SABR覆盖的内容）
    adaptive = sd.get('adaptiveFormats') or []
    if adaptive and any(f.get('url') for f in adaptive):
        video = _pick_adaptive(adaptive, 'video/')
        audio = _pick_adaptive(adaptive, 'audio/', prefer_itags=(140, 251))
        if video:
            return VideoInfo(title=title, video_url=video['url'],
                             audio_url=audio['url'] if audio else None)

    # 2. IOS HLS：免PO token获取高清分轨（支持画质选择）
    try:
        ios_data = await _player(client, 'ios', video_id)
        manifest_url = (ios_data.get('streamingData') or {}).get('hlsManifestUrl')
        if manifest_url:
            res = await req_retry(client, manifest_url)
            variants = _parse_hls_variants(res.text)
            video_url = _pick_hls_by_quality(variants, quality)
            audio_url = None
            for m in re.finditer(r'#EXT-X-MEDIA:[^\n]*', res.text):
                seg = m.group(0)
                if 'TYPE=AUDIO' not in seg:
                    continue
                uri = re.search(r'URI="([^"]+)"', seg)
                if uri and (audio_url is None or 'DEFAULT=YES' in seg.upper()):
                    audio_url = uri.group(1)
            return VideoInfo(title=title, video_url=video_url, audio_url=audio_url, hls=True)
    except (APIUnsupportedError, APIResourceError):
        raise
    except Exception:
        pass  # HLS路径失败时继续尝试muxed兜底

    # 3. muxed 兜底（itag 18, 360p mp4，音视频合一）
    muxed = _pick_adaptive(sd.get('formats') or [], 'video/', prefer_itags=(18,))
    if muxed and muxed.get('url'):
        return VideoInfo(title=title, video_url=muxed['url'], audio_url=None)
    raise APIUnsupportedError('未获取到任何可用视频流（SABR/风控），可尝试登录cookie', video_id)


async def get_captions(client: httpx.AsyncClient, url_or_id: str, langs: str = '') -> list:
    """获取YouTube字幕轨: [[json3下载地址, 语言名], ...]，langs为语言代码/名称包含过滤（逗号分隔）"""
    video_id = _parse_video_id(url_or_id) if str(url_or_id).startswith('http') else url_or_id
    data = await _player(client, 'android', video_id)
    tracks = _get_caption_tracks(data)
    result = [[f"{base}&fmt=json3", name] for base, name, _ in tracks]
    if langs:
        wanted = [v.strip().lower() for v in langs.split(',') if v.strip()]
        filtered = [[u, n] for (u, n), (_, name, code) in zip(result, tracks)
                    if any(v in f'{name} {code}'.lower() for v in wanted)]
        if not filtered:
            from bilix.log import logger
            logger.warning(f'没有匹配语言 "{langs}" 的YouTube字幕轨，可用: {[n for _, n in result]}')
        result = filtered
    return result


if __name__ == '__main__':
    import asyncio

    async def main():
        client = httpx.AsyncClient(**dft_client_settings)
        data = await get_video_info(client, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        print(data)

    asyncio.run(main())
