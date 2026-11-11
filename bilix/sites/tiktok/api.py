"""
Originally From
@Author: https://github.com/Evil0ctal/
https://github.com/Evil0ctal/Douyin_TikTok_Download_API

Modified by
@Author: https://github.com/HFrost0/

2026-11 重写：旧 api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed 已失效（429/空响应）。
新方案（实测）：网页 __UNIVERSAL_DATA_FOR_REHYDRATION__ 数据（含 WAF 挑战 PoW 解算兜底），
tikwm.com 第三方解析作为二级兜底（显式标注）。
下载 playAddr 需要：UA + Referer: https://www.tiktok.com/ + 会话cookie(tt_chain_token)，同client会话即可。
详见 docs/dev/site-support-status.md 平台方案记录。
"""
import re
import json
import base64
import hashlib
from typing import List
import httpx
from pydantic import BaseModel
from bilix.utils import legal_title
from bilix.download.utils import req_retry, raise_api_error
from bilix.exception import APIUnsupportedError, APIResourceError

UA_CHROME = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

dft_client_settings = {
    'headers': {'user-agent': UA_CHROME,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Site': 'none', 'Sec-Fetch-Dest': 'document',
                'Upgrade-Insecure-Requests': '1',
                # playAddr 下载要求 Referer，全局带上（对取数页无副作用）
                'Referer': 'https://www.tiktok.com/'},
    'http2': True
}


class VideoInfo(BaseModel):
    title: str
    author_name: str
    wm_urls: List[str]
    nwm_urls: List[str]
    cover: str
    dynamic_cover: str
    origin_cover: str


def _solve_waf_challenge(cs_b64: str) -> str:
    """解算TikTok WAF工作量证明挑战（算法同yt-dlp）：sha256(seed+i)穷举匹配摘要"""
    data = json.loads(base64.b64decode(cs_b64).decode())
    conf = data.get('v') if isinstance(data.get('v'), dict) else data
    seed, digest = conf['a'], conf['c']
    for i in range(1, 1_000_001):
        if hashlib.sha256(f'{seed}{i}'.encode()).hexdigest() == digest:
            conf['d'] = base64.b64encode(str(i).encode()).decode()
            return base64.b64encode(json.dumps(data).encode()).decode()
    raise APIUnsupportedError('TikTok WAF PoW求解失败（100万次内无匹配）', cs_b64[:60])


def _parse_page_url(url: str):
    """从url解析 (user, video_id)；兼容短链调用方已重定向的场景"""
    if m := re.search(r'tiktok\.com/@([\w.\-]+)/video/(\d+)', url):
        return m.group(1), m.group(2)
    if m := re.search(r'tiktok\.com/.*[?&]item_id=(\d+)', url):
        return 'video', m.group(1)
    raise APIUnsupportedError('无法从url解析视频id', url)


async def _get_item_from_page(client: httpx.AsyncClient, user: str, video_id: str) -> dict:
    url = f'https://www.tiktok.com/@{user}/video/{video_id}'
    res = await req_retry(client, url, follow_redirects=True)
    # WAF挑战页检测与解算重试（间歇性出现）
    if '<p id="cs">' in res.text or 'Please wait...' in res.text or '_wafchallengeid' in res.text:
        if m := re.search(r'<p id="cs"[^>]*>([^<]+)</p>', res.text):
            token = _solve_waf_challenge(m.group(1).strip())
            client.cookies.set('_wafchallengeid', token, domain='.tiktok.com')
            res = await req_retry(client, url, follow_redirects=True)
    m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', res.text, re.S)
    if not m:
        raise APIUnsupportedError('页面未含__UNIVERSAL_DATA__（可能被WAF拦截或结构变更）', url)
    data = json.loads(m.group(1))
    detail = (data.get('__DEFAULT_SCOPE__') or {}).get('webapp.video-detail') or {}
    item = (detail.get('itemInfo') or {}).get('itemStruct')
    if not item:
        status = detail.get('statusCode')
        raise APIResourceError(f'未获取到视频数据(statusCode={status})，可重试或稍后再试', video_id)
    return item


async def _get_item_from_tikwm(client: httpx.AsyncClient, page_url: str) -> dict:
    """第三方 tikwm.com 解析兜底（显式标注的第三方依赖，有频控风险）"""
    res = await req_retry(client, 'https://www.tikwm.com/api/', params={'url': page_url, 'hd': 1})
    data = res.json().get('data') or {}
    if not data.get('play'):
        raise APIResourceError(f'tikwm解析失败: {str(data)[:80]}', page_url)
    # 归一化为 itemStruct 形状，复用同一解析
    return {
        'desc': data.get('title', ''),
        'author': {'uniqueId': (data.get('author') or {}).get('unique_id', ''),
                   'nickname': (data.get('author') or {}).get('nickname', '')},
        'video': {
            'playAddr': data.get('hdplay') or data.get('play'),
            'downloadAddr': data.get('wmplay') or data.get('play'),
            'cover': data.get('cover', ''),
            'originCover': data.get('origin_cover', ''),
            'dynamicCover': data.get('dynamic_cover', ''),
        },
    }


@raise_api_error
async def get_video_info(client: httpx.AsyncClient, url: str) -> VideoInfo:
    if short_url := re.findall(r'https://www.tiktok.com/t/\w+/', url):
        res = await req_retry(client, short_url[0], follow_redirects=True)
        url = str(res.url)
    user, video_id = _parse_page_url(url)
    try:
        item = await _get_item_from_page(client, user, video_id)
    except (APIUnsupportedError, APIResourceError, httpx.HTTPError):
        # 网页方案失败（WAF高频/结构变更）时退回第三方兜底
        item = await _get_item_from_tikwm(client, url)
    # 视频标题 (如果为空则使用分享标题)
    title = legal_title(item.get('desc') or (item.get('share_meta') or {}).get('share_desc', video_id))
    author = item.get('author') or {}
    author_name = author.get('uniqueId') or author.get('nickname', '')
    video = item.get('video') or {}
    # playAddr 无水印 / downloadAddr 有水印；两者可能相同
    nwm_url = video.get('playAddr', '')
    wm_url = video.get('downloadAddr') or nwm_url
    cover = video.get('cover', '')
    video_info = VideoInfo(title=title, author_name=author_name,
                           wm_urls=[wm_url] if wm_url else [],
                           nwm_urls=[nwm_url] if nwm_url else [],
                           cover=cover,
                           dynamic_cover=video.get('dynamicCover') or cover,
                           origin_cover=video.get('originCover') or cover)
    return video_info


if __name__ == '__main__':
    import asyncio

    async def main():
        client = httpx.AsyncClient(**dft_client_settings)
        data = await get_video_info(client, 'https://www.tiktok.com/@lindaselection/video/7171715528124271877')
        print(data)

    asyncio.run(main())
