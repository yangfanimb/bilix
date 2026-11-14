import asyncio
import json
import re
from urllib.parse import quote
import httpx
from pydantic import field_validator, BaseModel, Field
from typing import Union, List, Tuple, Dict, Optional
import json5
from danmakuC.bilibili import parse_view
from bilix.download.utils import req_retry, raise_api_error
from bilix.log import logger
from bilix.sites.bilibili.utils import parse_ids_from_url
from bilix.utils import legal_title
from bilix.exception import APIInvalidError, APIError, APIResourceError, APIUnsupportedError
import hashlib
import time

dft_client_settings = {
    'headers': {'user-agent': 'PostmanRuntime/7.29.0', 'referer': 'https://www.bilibili.com'},
    'cookies': {'CURRENT_FNVAL': '4048'},
    'http2': True
}


@raise_api_error
async def get_cate_meta(client: httpx.AsyncClient) -> dict:
    """
    获取b站分区元数据

    :param client:
    :return:
    """
    cate_info = {}
    res = await req_retry(client, 'https://s1.hdslb.com/bfs/static/laputa-channel/client/assets/index.c0ea30e6.js')
    cate_data = re.search('Za=([^;]*);', res.text).groups()[0]
    cate_data = json5.loads(cate_data)['channelList']
    for i in cate_data:
        if 'sub' in i:
            for j in i['sub']:
                cate_info[j['name']] = j
            cate_info[i['name']] = i
    return cate_info


@raise_api_error
async def get_list_info(client: httpx.AsyncClient, url_or_sid: str, ):
    """
    获取视频列表信息

    :param url_or_sid:
    :param client:
    :return:
    """
    if url_or_sid.startswith('http'):
        sid = re.search(r'sid=(\d+)', url_or_sid).groups()[0]
    else:
        sid = url_or_sid
    res = await req_retry(client, f'https://api.bilibili.com/x/series/series?series_id={sid}')  # meta api
    meta = json.loads(res.text)
    mid = meta['data']['meta']['mid']
    params = {'mid': mid, 'series_id': sid, 'ps': meta['data']['meta']['total']}
    list_res, up_info = await asyncio.gather(
        req_retry(client, 'https://api.bilibili.com/x/series/archives', params=params),
        get_up_info(client, str(mid)),
    )
    list_info = json.loads(list_res.text)
    list_name = meta['data']['meta']['name']
    up_name = up_info.get('name', '')
    bvids = [i['bvid'] for i in list_info['data']['archives']]
    return list_name, up_name, bvids


@raise_api_error
async def get_collect_info(client: httpx.AsyncClient, url_or_sid: str):
    """
    获取合集信息

    :param url_or_sid:
    :param client:
    :return:
    """
    sid = re.search(r'sid=(\d+)', url_or_sid).groups()[0] if url_or_sid.startswith('http') else url_or_sid
    params = {'season_id': sid}
    res = await req_retry(client, 'https://api.bilibili.com/x/space/fav/season/list', params=params)
    data = json.loads(res.text)
    medias = data['data']['medias']
    info = data['data']['info']
    col_name, up_name = info['title'], medias[0]['upper']['name']
    bvids = [i['bvid'] for i in data['data']['medias']]
    return col_name, up_name, bvids


@raise_api_error
async def get_favour_page_info(client: httpx.AsyncClient, url_or_fid: str, pn=1, ps=20, keyword=''):
    """
    获取收藏夹信息（分页）

    :param url_or_fid:
    :param pn:
    :param ps:
    :param keyword:
    :param client:
    :return:
    """
    if url_or_fid.startswith('http'):
        fid = re.findall(r'fid=(\d+)', url_or_fid)[0]
    else:
        fid = url_or_fid
    params = {'media_id': fid, 'pn': pn, 'ps': ps, 'keyword': keyword, 'order': 'mtime'}
    res = await req_retry(client, 'https://api.bilibili.com/x/v3/fav/resource/list', params=params)
    data = json.loads(res.text)['data']
    fav_name, up_name = data['info']['title'], data['info']['upper']['name']
    total_size = data['info']['media_count']
    if data.get('medias') is None:  # 收藏夹不可见：私密收藏夹或无权限访问
        raise APIResourceError("收藏夹内容不可见（可能为私密收藏夹，或需要登录后访问）", url_or_fid)
    bvids = [i['bvid'] for i in data['medias'] if i['title'] != '已失效视频']
    return fav_name, up_name, total_size, bvids


@raise_api_error
async def get_cate_page_info(client: httpx.AsyncClient, cate_id, time_from, time_to, pn=1, ps=30,
                             order='click', keyword='', main_cate_id=None):
    """
    获取分区视频信息（分页）。旧的 s.search.bilibili.com/cate/search 接口已失效：
    - 有关键词时使用 wbi search/type 接口（支持分区过滤、排序、发布时间过滤），
      注意该接口的tids仅支持主分区，子分区请传入main_cate_id
    - 无关键词时 search/type 会返回-400，改用 newlist 接口获取分区最新投稿（不支持排序与时间过滤）

    :param cate_id: 分区tid，支持子分区（仅无关键词时使用）
    :param pn: 页码
    :param ps: 页大小
    :param order: 排序方式，仅关键词搜索时生效：pubdate发布时间，click播放数，stow收藏数，dm弹幕数
    :param time_from: 开始日期，%Y%m%d格式字符串或时间戳（仅关键词搜索时生效）
    :param time_to: 结束日期，格式同上
    :param keyword: 搜索关键词，为空时使用分区最新投稿列表
    :param main_cate_id: keyword搜索时使用的主分区tid，子分区搜索时由调用方传入
    :param client:
    :return:
    """
    keyword = (keyword or '').strip()
    if keyword:
        supported_orders = {'click', 'pubdate', 'dm', 'stow'}
        base_params = {'search_type': 'video', 'keyword': keyword, 'page': pn, 'page_size': ps,
                       'order': order if order in supported_orders else 'totalrank'}
        # 时间过滤参数为unix秒
        for key, val in (('pubtime_begin_s', time_from), ('pubtime_end_s', time_to)):
            if isinstance(val, str):  # %Y%m%d格式
                val = int(time.mktime(time.strptime(val, '%Y%m%d')))
            base_params[key] = int(val)
        params = {**base_params, 'tids': int(main_cate_id or cate_id)}
        await _add_sign(client, params)
        res = await req_retry(client, 'https://api.bilibili.com/x/web-interface/wbi/search/type', params=params)
        info = json.loads(res.text)
        result = (info.get('data') or {}).get('result') or []
        if not result:  # 部分旧子分区tid在search接口中无效，降级为全站关键词搜索
            logger.warning(f"tids={params['tids']} 在搜索接口中无结果，降级为全站关键词搜索")
            params = {k: v for k, v in base_params.items()}
            await _add_sign(client, params)
            res = await req_retry(client, 'https://api.bilibili.com/x/web-interface/wbi/search/type', params=params)
            info = json.loads(res.text)
            result = (info.get('data') or {}).get('result') or []
        bvids = [i['bvid'] for i in result if i.get('bvid')]
    else:
        res = await req_retry(client, 'https://api.bilibili.com/x/web-interface/newlist',
                              params={'main_ver': 'v3', 'search_type': 'video', 'cate_id': int(cate_id),
                                      'pn': pn, 'ps': ps})
        info = json.loads(res.text)
        archives = (info.get('data') or {}).get('archives') or []
        bvids = [i['bvid'] for i in archives]
    return bvids


async def _ensure_buvid(client: httpx.AsyncClient):
    """确保client携带buvid3/buvid4 cookie，降低wbi接口412风控概率

    :param client:
    :return:
    """
    if client.cookies.get('buvid3'):
        return
    try:
        res = await req_retry(client, 'https://api.bilibili.com/x/frontend/finger/spi')
        data = json.loads(res.text)['data']
        client.cookies.update({'buvid3': data['b_3'], 'buvid4': data['b_4']})
    except Exception as e:  # 获取失败不影响后续流程，best effort
        logger.debug(f"get buvid failed: {e}")


async def _add_sign(client: httpx.AsyncClient, params: dict):
    """添加b站api签名到params中
    :param params:
    :return:
    """
    await _ensure_buvid(client)
    OE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45,
          35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
          41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60,
          51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
          20, 34, 44, 52]
    res = await req_retry(
        client, "https://api.bilibili.com/x/web-interface/nav"
    )
    info = json.loads(res.text)
    img_val = info['data']['wbi_img']['img_url'].split('/')[-1].split('.')[0]
    sub_val = info['data']['wbi_img']['sub_url'].split('/')[-1].split('.')[0]
    val = img_val + sub_val
    request_token = ''.join([val[v] for v in OE])[:32]

    wts = int(time.time())
    params["wts"] = wts
    data = dict(sorted(params.items()))
    data_str = "&".join([f"{k}={v}" for k, v in data.items()]) + request_token
    md5 = hashlib.md5(data_str.encode("utf-8")).hexdigest()
    params["w_rid"] = md5
    return params


def _find_mid(space_url: str):
    return re.search(r'^https://space.bilibili.com/(\d+)/?', space_url).group(1)


@raise_api_error
async def get_up_video_info(client: httpx.AsyncClient, url_or_mid: str, pn=1, ps=30, order="pubdate", keyword=""):
    """
    获取up主信息

    :param url_or_mid:
    :param pn:
    :param ps:
    :param order:
    :param keyword:
    :param client:
    :return:
    """
    if url_or_mid.startswith("http"):
        mid = re.findall(r"/(\d+)", url_or_mid)[0]
    else:
        mid = url_or_mid

    params = {"mid": mid, "order": order, "ps": ps, "pn": pn, "keyword": quote(keyword or "")}
    await _add_sign(client, params)

    res = await req_retry(client, "https://api.bilibili.com/x/space/wbi/arc/search", params=params)
    info = json.loads(res.text)
    up_name = info["data"]["list"]["vlist"][0]["author"]
    total_size = info["data"]["page"]["count"]
    bv_ids = [i["bvid"] for i in info["data"]["list"]["vlist"]]
    return up_name, total_size, bv_ids


async def get_up_info(client: httpx.AsyncClient, url_or_mid: str):
    if url_or_mid.startswith("http"):
        mid = _find_mid(url_or_mid)
    else:
        mid = url_or_mid
    params = {"mid": mid}
    await _add_sign(client, params)
    res = await req_retry(client, "https://api.bilibili.com/x/space/wbi/acc/info", params=params)
    data = json.loads(res.text)['data']
    return data


class Media(BaseModel):
    base_url: str
    backup_url: Optional[List[str]] = None
    size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    suffix: Optional[str] = None
    quality: Optional[str] = None
    codec: Optional[str] = None
    segment_base: Optional[dict] = None

    @property
    def urls(self):
        """the copy of all url including backup"""
        return [self.base_url, *self.backup_url] if self.backup_url else [self.base_url]


class Dash(BaseModel):
    duration: int
    videos: List[Media]
    audios: List[Media]
    video_formats: Dict[str, Dict[str, Media]]
    audio_formats: Dict[str, Optional[Media]]

    @classmethod
    def from_dict(cls, play_info: dict):
        dash = play_info['dash']  # may raise KeyError
        video_formats = {}
        quality_map = {}
        for d in play_info['support_formats']:
            quality_map[d['quality']] = d['new_description']
            video_formats[d['new_description']] = {}
        videos = []
        for d in dash['video']:
            if d['id'] not in quality_map:
                continue  # https://github.com/HFrost0/bilix/issues/93
            quality = quality_map[d['id']]
            m = Media(quality=quality, codec=d['codecs'], **d)
            video_formats[quality][m.codec] = m
            videos.append(m)

        audios = []
        audio_formats = {}
        if dash.get('audio', None):  # some video have NO audio
            # 列表顺序不保证（实测存在[132k,192k,64k]的排列），按bandwidth取最高音质
            d = max(dash['audio'], key=lambda x: x.get('bandwidth', 0))
            m = Media(quality="default", suffix='.aac', codec=d['codecs'], **d)
            audios.append(m)
            audio_formats[m.quality] = m
        if dash['dolby']['type'] != 0:
            quality = "dolby"
            audio_formats[quality] = None
            if dash['dolby'].get('audio', None):
                d = dash['dolby']['audio'][0]
                m = Media(quality=quality, suffix='.eac3', codec=d['codecs'], **d)
                audios.append(m)
                audio_formats[m.quality] = m
        if dash.get('flac', None):
            quality = "flac"
            audio_formats[quality] = None
            if d := dash['flac']['audio']:
                m = Media(quality=quality, suffix='.flac', codec=d['codecs'], **d)
                audios.append(m)
                audio_formats[m.quality] = m
        return cls(duration=dash['duration'], videos=videos, audios=audios,
                   video_formats=video_formats, audio_formats=audio_formats)

    def choose_video(self, quality: Union[int, str], video_codec: str) -> Media:
        # 1. absolute choice with quality name like 4k 1080p '1080p 60帧'
        if isinstance(quality, str):
            for k in self.video_formats:
                if k.upper().startswith(quality.upper()):  # incase 1080P->1080p
                    for c in self.video_formats[k]:
                        if c.startswith(video_codec):
                            return self.video_formats[k][c]
        # 2. relative choice
        else:
            keys = [k for k in self.video_formats.keys() if self.video_formats[k]]
            quality = min(quality, len(keys) - 1)
            k = keys[quality]
            for c in self.video_formats[k]:
                if c.startswith(video_codec):
                    return self.video_formats[k][c]
        raise KeyError(f"no match for video quality: {quality} codec: {video_codec}")

    def choose_audio(self, audio_codec: str) -> Optional[Media]:
        if len(self.audios) == 0:  # some video has no audio
            return
        for k in self.audio_formats:
            if self.audio_formats[k] and self.audio_formats[k].codec.startswith(audio_codec):
                return self.audio_formats[k]
        raise KeyError(f'no match for audio codec: {audio_codec}')

    def choose_quality(self, quality: Union[str, int], codec: str = '') -> Tuple[Media, Optional[Media]]:
        v_codec, a_codec, *_ = codec.split(':') + [""]
        video, audio = self.choose_video(quality, v_codec), self.choose_audio(a_codec)
        return video, audio


class Status(BaseModel):
    view: int = Field(description="播放量")
    danmaku: int = Field(description="弹幕数")
    coin: int = Field(description="硬币数")
    like: int = Field(description="点赞数")
    reply: int = Field(description="回复数")
    favorite: int = Field(description="收藏数")
    share: int = Field(description="分享数")
    follow: Optional[int] = Field(default=None, description="追剧数/追番数")

    @field_validator('view', mode="before")
    @classmethod
    def no_view(cls, v):
        return 0 if v == '--' else v


class Page(BaseModel):
    p_name: str
    p_url: str


class VideoInfo(BaseModel):
    title: str
    aid: int
    cid: int
    ep_id: Optional[int] = None
    p: int
    pages: List[Page]  # [[p_name, p_url], ...]
    img_url: str
    status: Status
    bvid: Optional[str] = None
    dash: Optional[Dash] = None
    other: Optional[List[Media]] = None  # durl resource: flv, mp4.
    desc: Optional[str] = None
    tags: Optional[List[str]] = None


def _parse_bv_html(url, html: str) -> VideoInfo:
    init_info = re.search(r'<script>window.__INITIAL_STATE__=({.*?});\(', html).groups()[0]  # this line may raise
    init_info = json.loads(init_info)
    if len(init_info.get('error', {})) > 0:
        raise APIResourceError("视频已失效", url)  # 啊叻？视频不见了？在分区下载的时候可能产生
    # extract meta
    pages = []
    h1_title = legal_title(re.search('<h1[^>]*title="([^"]*)"', html).groups()[0])
    status = Status(**init_info['videoData']['stat'])
    bvid = init_info['bvid']
    desc = init_info['videoData'].get('desc', '')
    tags = [i['tag_name'] for i in init_info['tags']]
    aid = init_info['aid']
    (p, cid), = init_info['cidMap'][bvid]['cids'].items()
    p = int(p) - 1
    title = legal_title(init_info['videoData']['title'])
    base_url = url.split('?')[0]
    for idx, i in enumerate(init_info['videoData']['pages']):
        p_url = f"{base_url}?p={idx + 1}"
        p_name = f"P{idx + 1}-{i['part']}" if len(init_info['videoData']['pages']) > 1 else ''
        pages.append(Page(p_name=p_name, p_url=p_url))
    # extract dash and flv_url
    dash, other = None, []
    play_info = re.search('<script>window.__playinfo__=({.*?})</script><script>', html).groups()[0]
    play_info = json.loads(play_info)['data']
    try:
        dash = Dash.from_dict(play_info)
    except KeyError:
        pass
    try:
        for i in play_info['durl']:
            suffix = re.search(r'\.([a-zA-Z0-9]+)\?', i['url']).group(1)
            other.append(Media(base_url=i['url'], backup_url=i['backup_url'], suffix=suffix))
    except KeyError:
        pass
    # extract img url
    img_url = re.search('property="og:image" content="([^"]*)"', html).groups()[0]
    if not img_url.startswith('http'):  # https://github.com/HFrost0/bilix/issues/52 just for some video
        img_url = 'http:' + img_url.split('@')[0]
    # construct data
    video_info = VideoInfo(title=title, aid=aid, cid=cid, status=status,
                           p=p, pages=pages, img_url=img_url, bvid=bvid, dash=dash, other=other,
                           desc=desc, tags=tags)
    return video_info


def _parse_ep_from_season(url: str, season_data: dict) -> VideoInfo:
    """通过 pgc/view/web/season api 的数据构建番剧 VideoInfo

    :param url: 番剧播放页url，用于定位用户选择的ep
    :param season_data: season api 返回的 result 数据
    :return:
    """
    status_d = season_data['stat']
    status = Status(coin=status_d['coins'], view=status_d['views'], danmaku=status_d['danmakus'],
                    share=status_d['share'], like=status_d['likes'], reply=status_d['reply'],
                    favorite=status_d['favorite'], follow=status_d['favorites'])
    title = legal_title(season_data['title'])
    desc = season_data.get('evaluate', '')
    # 番剧正片列表，部分剧集（如预告、pv）可能存在于sections中
    episodes = list(season_data.get('episodes') or [])
    if not episodes:
        for section in season_data.get('sections') or []:
            episodes.extend(section.get('episodes') or [])
    if not episodes:
        raise APIResourceError("未找到剧集数据", url)
    m = re.search(r'/bangumi/play/(ep|ss)(\d+)', url)
    ep_id = m.group(2) if m and m.group(1) == 'ep' else None
    p, aid, cid, bvid, img_url = 0, 0, 0, "", ''
    ep_found = False
    pages = []
    for i, ep in enumerate(episodes):
        if ep_id is not None and str(ep["id"]) == ep_id:
            p = i
            ep_found = True
            aid, cid, bvid = int(ep["aid"]), int(ep["cid"]), ep.get("bvid") or ""
            img_url = ep.get("cover") or ''
        p_name = legal_title(ep.get('long_title') or ep.get('show_title') or ep['title'])
        if len(episodes) > 1 and not p_name:  # 避免多集文件名冲突
            p_name = f"P{i + 1}"
        pages.append(Page(p_name=p_name, p_url=ep["link"]))
    if ep_id is not None and not ep_found:
        raise APIResourceError("未找到该集数", url)
    if ep_id is None:  # ss url 取第一集
        ep0 = episodes[0]
        aid, cid, bvid, img_url = int(ep0["aid"]), int(ep0["cid"]), ep0.get("bvid") or "", ep0.get("cover") or ''
        ep_id = str(ep0["id"])
    if not img_url:
        img_url = season_data.get('cover', '')
    video_info = VideoInfo(
        title=title, status=status, desc=desc,
        aid=aid, cid=cid, bvid=bvid, p=p, pages=pages,
        img_url=img_url, ep_id=int(ep_id),
    )
    return video_info


@raise_api_error
async def get_season_info(client: httpx.AsyncClient, url_or_sid: str) -> dict:
    """获取番剧season信息

    :param url_or_sid: 番剧播放页url或season_id
    :param client:
    :return: season api 返回的 result 数据
    """
    if str(url_or_sid).startswith('http'):
        m = re.search(r'/bangumi/play/(ep|ss)(\d+)', url_or_sid)
        if not m:
            raise APIInvalidError("无法从url中解析番剧id", url_or_sid)
        params = {'ep_id': m.group(2)} if m.group(1) == 'ep' else {'season_id': m.group(2)}
    else:
        params = {'season_id': url_or_sid}
    res = await req_retry(client, 'https://api.bilibili.com/pgc/view/web/season', params=params)
    data = json.loads(res.text)
    if data['code'] != 0:
        raise APIResourceError(data.get('message', 'unknown'), url_or_sid)
    return data['result']


@raise_api_error
async def get_video_info(client: httpx.AsyncClient, url: str) -> VideoInfo:
    if '/bangumi/play/' in url:
        # 番剧页面已不再内嵌剧集数据，直接通过season api获取
        season_data = await get_season_info(client, url)
        video_info = _parse_ep_from_season(url, season_data)
        await _attach_ep_dash(client, video_info)
        return video_info
    try:
        # try to get video info from web front-end first
        return await _get_video_info_from_html(client, url)
    except APIInvalidError:
        # try to get video info from api if web front-end is banned
        return await _get_video_info_from_api(client, url)


async def _get_video_info_from_html(client: httpx.AsyncClient, url: str) -> VideoInfo:
    res = await req_retry(client, url, follow_redirects=True)
    if str(res.url).startswith("https://www.bilibili.com/festival"):
        raise APIInvalidError("特殊节日页面", url)
    html = res.text
    if "window._riskdata_" in html:
        raise APIInvalidError("web 前端访问被风控", url)
    if "window.__INITIAL_STATE__" in html:
        return _parse_bv_html(url, html)
    else:
        raise APIUnsupportedError("未知页面类型", url)


async def _get_video_info_from_api(client: httpx.AsyncClient, url: str) -> VideoInfo:
    assert '/av' in url or '/BV' in url  # TODO: only support BV or av url
    video_info = await _get_video_basic_info_from_api(client, url)
    # can not be parallelized since we need to get cid first
    await _attach_dash_and_durl_from_api(client, video_info)
    return video_info


async def _attach_ep_dash(client: httpx.AsyncClient, video_info: VideoInfo):
    params = {
        'support_multi_audio': True,
        'avid': video_info.aid,
        'cid': video_info.cid,
        'fnver': 0,
        'fnval': 4048,
        'fourk': 1,
        'ep_id': video_info.ep_id,
    }
    res = await req_retry(client, 'https://api.bilibili.com/pgc/player/web/v2/playurl', params=params)
    res = json.loads(res.text)
    data = res['result']['video_info']
    if "dash" in data:
        video_info.dash = Dash.from_dict(data)
    if "durl" in data:
        other = []
        for i in data['durl']:
            suffix = re.search(r'\.([a-zA-Z0-9]+)\?', i['url']).group(1)
            other.append(Media(base_url=i['url'], backup_url=i['backup_url'], size=i['size'], suffix=suffix))
        video_info.other = other


async def _attach_dash_and_durl_from_api(client: httpx.AsyncClient, video_info: VideoInfo):
    params = {'cid': video_info.cid, 'bvid': video_info.bvid,
              'qn': 120,  # 如无 dash 资源（少数老视频），fallback 到 4K 超清 durl
              'fnval': 4048,  # 如 dash 资源可用，请求 dash 格式的全部可用流
              'fourk': 1,  # 请求 4k 资源
              'fnver': 0, 'platform': 'pc', 'otype': 'json'}
    dash_response = await req_retry(client, 'https://api.bilibili.com/x/player/playurl',
                                    params=params, follow_redirects=True)
    dash_json = json.loads(dash_response.text)
    if dash_json['code'] != 0:
        raise APIResourceError(dash_json['message'], video_info.bvid)
    dash, other = None, []
    if 'dash' in dash_json['data']:
        dash = Dash.from_dict(dash_json['data'])
    if 'durl' in dash_json['data']:
        for i in dash_json['data']['durl']:
            suffix = re.search(r'\.([a-zA-Z0-9]+)\?', i['url']).group(1)
            other.append(Media(base_url=i['url'], backup_url=i['backup_url'], size=i['size'], suffix=suffix))
    video_info.dash, video_info.other = dash, other


async def _get_video_basic_info_from_api(client: httpx.AsyncClient, url) -> VideoInfo:
    """通过 view api 获取视频的基本信息，不包括 dash 或 durl(other) 视频流资源"""
    aid, bvid, selected_page_num = parse_ids_from_url(url)
    params = {'bvid': bvid} if bvid else {'aid': aid}
    r = await req_retry(client, 'https://api.bilibili.com/x/web-interface/view',
                        params=params, follow_redirects=True)
    raw_json = json.loads(r.text)
    if raw_json['code'] != 0:
        raise APIResourceError(raw_json['message'], raw_json['message'])
    title = legal_title(raw_json['data']['title'])
    h1_title = title  # TODO: 根据视频类型，使 h1_title 与实际网页标题的格式一致
    aid = raw_json['data']['aid']
    bvid = raw_json['data']['bvid']
    base_url = f"https://www.bilibili.com/video/{bvid}/"
    status = Status(**raw_json['data']['stat'])
    pages = []
    p = None
    cid = None
    for idx, i in enumerate(raw_json['data']['pages']):
        page_num = int(i['page'])
        if page_num == selected_page_num:
            p = idx  # selected_page_num 的分p 在 pages 列表中的 index 位置
            cid = int(i['cid'])  # selected_page_num 的分p 的 cid
        p_url = f"{base_url}?p={page_num}"
        p_name = f"P{page_num}-{i['part']}"
        pages.append(Page(p_name=p_name, p_url=p_url))
    assert p is not None, f"没有找到分P: p{selected_page_num}，请检查输入"  # cid 也会是 None
    img_url = raw_json['data']['pic']
    basic_video_info = VideoInfo(title=title, h1_title=h1_title, aid=aid, cid=cid, status=status,
                                 p=p, pages=pages, img_url=img_url, bvid=bvid, dash=None, other=None)
    return basic_video_info


def _norm_subtitle_url(url: str) -> str:
    """字幕地址规范化：兼容协议相对(//host)、完整URL、空值等形态"""
    if not url:
        return ''
    if url.startswith('http'):
        return url
    if url.startswith('//'):
        return 'https:' + url
    return 'https://' + url.lstrip('/')


@raise_api_error
async def get_subtitle_info(client: httpx.AsyncClient, bvid, cid, langs: str = ''):
    """获取字幕轨信息: [[规范化url, 语言名, 语言代码], ...]

    :param bvid:
    :param cid:
    :param langs: 语言过滤，逗号分隔（对lan代码与lan_doc做包含匹配，如 '中文,en'），空为全部
    :param client:
    """
    params = {'bvid': bvid, 'cid': cid}
    res = await req_retry(client, 'https://api.bilibili.com/x/player/v2', params=params)
    info = json.loads(res.text)
    if info['code'] == -400:
        raise APIError(f'未找到字幕信息', params)
    tracks = []  # (规范化url, 语言名, 语言代码)
    for i in info['data']['subtitle']['subtitles']:
        url = _norm_subtitle_url(i.get('subtitle_url', ''))
        if not url:  # AI字幕偶发返回空地址，跳过避免拼出非法URL
            logger.warning(f'字幕轨 {i.get("lan_doc", "?")} 地址为空，跳过')
            continue
        tracks.append((url, i['lan_doc'], i.get('lan', '')))
    result = [[u, n, lan] for u, n, lan in tracks]
    if langs:
        wanted = [v.strip().lower() for v in langs.split(',') if v.strip()]
        result = [[u, n, lan] for u, n, lan in tracks
                  if any(v in f'{n} {lan}'.lower() for v in wanted)]
        if not result:
            # AI字幕轨会动态生成/波动，这里降级为debug；调用方可配合轮询等待
            logger.debug(f'没有匹配语言 "{langs}" 的字幕轨，可用: {[n for _, n, _ in tracks]}')
    return result


@raise_api_error
async def get_dm_urls(client: httpx.AsyncClient, aid, cid) -> List[str]:
    params = {'oid': cid, 'pid': aid, 'type': 1}
    res = await req_retry(client, f'https://api.bilibili.com/x/v2/dm/web/view', params=params)
    view = parse_view(res.content)
    total = int(view['dmSge']['total'])
    return [f'https://api.bilibili.com/x/v2/dm/web/seg.so?oid={cid}&type=1&segment_index={i + 1}' for i in range(total)]
