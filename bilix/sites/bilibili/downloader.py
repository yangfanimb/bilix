import asyncio
import functools
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional, Union, Sequence, Tuple, List
import aiofiles
import httpx
from datetime import datetime, timedelta
from . import api
from bilix.download.base_downloader_part import BaseDownloaderPart
from bilix._process import SingletonPPE
from bilix.utils import legal_title, cors_slice, valid_sess_data, t2s, json2srt
from bilix.download.base_downloader import BaseDownloader, TaskReport
from bilix.download.utils import req_retry, path_check, eclipse_str
from bilix.exception import HandleMethodError, APIUnsupportedError, APIResourceError, APIError
from bilix.cli.assign import kwargs_filter, auto_assemble
from bilix import ffmpeg

from danmakuC.bilibili import proto2ass


class DownloaderBilibili(BaseDownloaderPart):
    cookie_domain = "bilibili.com"  # for load cookies quickly
    pattern = re.compile(r"^https?://([A-Za-z0-9-]+\.)*(bilibili\.com|b23\.tv)")

    def __init__(
            self,
            *,
            client: httpx.AsyncClient = None,
            browser: str = None,
            speed_limit: Union[float, int, None] = None,
            stream_retry: int = 5,
            progress=None,
            logger=None,
            part_concurrency: int = 10,
            # unique params
            sess_data: str = None,
            video_concurrency: Union[int, asyncio.Semaphore] = 3,
            hierarchy: bool = True,
            sub_lang: str = '',
            audio_format: str = 'aac',
    ):
        """

        :param client:
        :param browser:
        :param speed_limit:
        :param stream_retry:
        :param progress:
        :param logger:
        :param sess_data: bilibili SESSDATA cookie
        :param part_concurrency: 媒体分段并发数
        :param video_concurrency: 视频并发数
        :param hierarchy: 是否使用层级目录
        :param sub_lang: 字幕语言过滤，逗号分隔（如 '中文,en'），空为下载全部字幕轨
        :param audio_format: 仅下载音频(-oa)时的输出格式: aac=原始ADTS流, m4a=无损重封装(时长元数据正确), mp3=转码(需libmp3lame)
        """
        client = client or httpx.AsyncClient(**api.dft_client_settings)
        super(DownloaderBilibili, self).__init__(
            client=client,
            browser=browser,
            speed_limit=speed_limit,
            stream_retry=stream_retry,
            progress=progress,
            logger=logger,
            part_concurrency=part_concurrency,
        )
        client.cookies.set('SESSDATA', valid_sess_data(sess_data))
        self._cate_meta = None
        self.v_sema = asyncio.Semaphore(video_concurrency)
        self.api_sema = asyncio.Semaphore(video_concurrency)
        self.hierarchy = hierarchy
        self.title_overflow = 50
        # 字幕语言过滤（--sub-lang），如 '中文,en'；空为下载全部字幕轨
        self.sub_lang = sub_lang or ''
        # 仅下载音频(-oa)时的输出格式: aac原始流 / m4a无损重封装 / mp3转码
        self.audio_format = (audio_format or 'aac').lower()
        # 下载任务执行报告收集器（供CLI结束时输出总结）
        self.reports: List[TaskReport] = []
        # 跨运行状态记忆：重跑同一命令时自动跳过上次成功项、重试失败项
        self._state_path = None
        self._prev_state: dict = {}
        self._url_state: dict = {}
        
    @classmethod
    def parse_url(cls, url: str):
        if re.match(r'https://space\.bilibili\.com/\d+/favlist\?fid=\d+', url):
            return cls.get_favour
        elif re.match(r'https://space\.bilibili\.com/\d+/channel/seriesdetail\?sid=\d+', url):
            return cls.get_collect_or_list
        elif re.match(r'https://space\.bilibili\.com/\d+/channel/collectiondetail\?sid=\d+', url):
            return cls.get_collect_or_list
        elif re.match(r'https://space\.bilibili\.com/\d+', url):  # up space url
            return cls.get_up
        elif re.search(r'(www\.bilibili\.com)|(b23\.tv)', url):
            return cls.get_video
        raise ValueError(f'{url} no match for bilibili')

    async def get_collect_or_list(self, url, path=Path('.'),
                                  quality=0, image=False, subtitle=False, dm=False, only_audio=False, codec: str = ''):
        """
        下载合集或视频列表
        :cli: short: col
        :param url: 合集或视频列表详情页url
        :param path: 保存路径
        :param quality:
        :param image:
        :param subtitle:
        :param dm:
        :param only_audio:
        :param codec:
        :return:
        """
        if 'series' in url:
            list_name, up_name, bvids = await api.get_list_info(self.client, url)
            name = legal_title(f"【视频列表】{up_name}", list_name)
        elif 'collection' in url:
            col_name, up_name, bvids = await api.get_collect_info(self.client, url)
            name = legal_title(f"【合集】{up_name}", col_name)
        else:
            raise ValueError(f'{url} invalid for get_collect_or_list')
        self._init_state(f'collect:{url}')
        if self.hierarchy:
            path /= name
            path.mkdir(parents=True, exist_ok=True)
        await asyncio.gather(
            *[self.get_series(f"https://www.bilibili.com/video/{i}", path=path, quality=quality, codec=codec,
                              image=image, subtitle=subtitle, dm=dm, only_audio=only_audio)
              for i in bvids])
        self._save_state()

    async def get_favour(self, url_or_fid, path=Path('.'),
                         num=20, keyword='', quality=0, series=True, image=False, subtitle=False,
                         dm=False, only_audio=False, codec: str = ''):
        """
        下载收藏夹内的视频
        :cli: short: fav
        :param url_or_fid: 收藏夹url或收藏夹id
        :param path: 保存路径
        :param num: 下载数量
        :param keyword: 搜索关键词
        :param quality: 画面质量，0为可以观看的最高画质，越大质量越低，超过范围时自动选择最低画质，或者直接使用字符串指定'1080p'等名称
        :param series: 每个视频是否下载所有p，False时仅下载系列中的第一个视频
        :param image: 是否下载封面
        :param subtitle: 是否下载字幕
        :param dm: 是否下载弹幕
        :param only_audio: 是否仅下载音频
        :param codec:
        :return:
        """
        self._init_state(f'favour:{url_or_fid}')
        try:
            fav_name, up_name, total_size, bvids = await api.get_favour_page_info(self.client, url_or_fid, keyword=keyword)
        except APIError as e:  # 收藏夹不可见等场景，避免向CLI抛出裸异常
            return self.logger.error(e)
        if self.hierarchy:
            name = legal_title(f"【收藏夹】{up_name}-{fav_name}")
            path /= name
            path.mkdir(parents=True, exist_ok=True)
        total = min(total_size, num)
        ps = 20
        page_nums = total // ps + min(1, total % ps)
        cors = []
        for i in range(page_nums):
            if i + 1 == page_nums:
                num = total - (page_nums - 1) * ps
            else:
                num = ps
            cors.append(self._get_favor_by_page(
                url_or_fid, path, i + 1, num, keyword, quality, series, image, subtitle, dm, only_audio, codec=codec))
        await asyncio.gather(*cors)
        self._save_state()

    async def _get_favor_by_page(self, url_or_fid, path: Path, pn=1, num=20, keyword='', quality=0,
                                 series=True, image=False, subtitle=False, dm=False, only_audio=False, codec=''):
        ps = 20
        num = min(ps, num)
        try:
            _, _, _, bvids = await api.get_favour_page_info(self.client, url_or_fid, pn, ps, keyword)
        except APIError as e:
            return self.logger.error(e)
        cors = []
        for i in bvids[:num]:
            func = self.get_series if series else self.get_video
            # noinspection PyArgumentList
            cors.append(func(f'https://www.bilibili.com/video/{i}', path=path, quality=quality, codec=codec,
                             image=image, subtitle=subtitle, dm=dm, only_audio=only_audio))
        await asyncio.gather(*cors)

    @property
    async def cate_meta(self):
        if not self._cate_meta:
            self._cate_meta = asyncio.ensure_future(api.get_cate_meta(self.client))
            self._cate_meta = await self._cate_meta
        elif asyncio.isfuture(self._cate_meta):
            await self._cate_meta
        return self._cate_meta

    async def get_cate(self, cate_name: str, path=Path('.'), num=10, order='click', keyword='', days=7,
                       quality=0, series=True, image=False, subtitle=False, dm=False, only_audio=False, codec='', ):
        """
        下载分区视频
        :cli: short: cate
        :param cate_name: 分区名称
        :param path: 保存路径
        :param num: 下载数量
        :param order: 何种排序，click播放数，scores评论数，stow收藏数，coin硬币数，dm弹幕数
        :param keyword: 搜索关键词
        :param days: 过去days天中的结果
        :param quality: 画面质量，0为可以观看的最高画质，越大质量越低，超过范围时自动选择最低画质，或者直接使用字符串指定'1080p'等名称
        :param series: 每个视频是否下载所有p，False时仅下载系列中的第一个视频
        :param image: 是否下载封面
        :param subtitle: 是否下载字幕
        :param dm: 是否下载弹幕
        :param only_audio: 是否仅下载音频
        :param codec:
        :return:
        """
        cate_meta = await self.cate_meta
        if cate_name not in cate_meta:
            return self.logger.error(f'未找到分区 {cate_name}')
        if 'subChannelId' not in cate_meta[cate_name]:
            sub_names = [i['name'] for i in cate_meta[cate_name]['sub']]
            return self.logger.error(f'{cate_name} 是主分区，仅支持子分区，试试 {sub_names}')
        self._init_state(f'cate:{cate_name}')
        if self.hierarchy:
            path /= legal_title(f"【分区】{cate_name}")
            path.mkdir(parents=True, exist_ok=True)
        cate_id = cate_meta[cate_name]['tid']
        # search接口的tids仅支持主分区，为子分区时找到所属主分区tid
        main_cate_id = next((i['tid'] for i in cate_meta.values()
                             if 'sub' in i and any(s.get('tid') == cate_id for s in i['sub'])), cate_id)
        time_to = datetime.now()
        time_from = time_to - timedelta(days=days)
        time_from, time_to = time_from.strftime('%Y%m%d'), time_to.strftime('%Y%m%d')
        pagesize = 30
        page = 1
        cors = []
        while num > 0:
            cors.append(self._get_cate_by_page(
                cate_id, path, time_from, time_to, page, min(pagesize, num), order, keyword, quality,
                series, image=image, subtitle=subtitle, dm=dm, only_audio=only_audio, codec=codec,
                main_cate_id=main_cate_id))
            num -= pagesize
            page += 1
        await asyncio.gather(*cors)
        self._save_state()

    async def _get_cate_by_page(
            self, cate_id, path: Path, time_from, time_to, pn=1, num=30, order='click', keyword='',
            quality=0, series=True, image=False, subtitle=False, dm=False, only_audio=False, codec='',
            main_cate_id=None):
        bvids = await api.get_cate_page_info(self.client, cate_id, time_from, time_to, pn, 30, order, keyword,
                                             main_cate_id=main_cate_id)
        bvids = bvids[:num]
        func = self.get_series if series else self.get_video
        # noinspection PyArgumentList
        cors = [func(f"https://www.bilibili.com/video/{i}", path=path, quality=quality, codec=codec,
                     image=image, subtitle=subtitle, dm=dm, only_audio=only_audio)
                for i in bvids]
        await asyncio.gather(*cors)

    async def get_up(
            self, url_or_mid: str, path=Path('.'), num=10, order='pubdate', keyword='', quality=0,
            series=True, image=False, subtitle=False, dm=False, only_audio=False, codec='', ):
        """
        下载up主视频
        :cli: short: up
        :param url_or_mid: b站用户空间页面url 或b站用户id，在空间页面的url中可以找到
        :param path: 保存路径
        :param num: 下载总数
        :param order: 何种排序，b站支持：最新发布pubdate，最多播放click，最多收藏stow
        :param keyword: 过滤关键词
        :param quality: 画面质量，0为可以观看的最高画质，越大质量越低，超过范围时自动选择最低画质，或者直接使用字符串指定'1080p'等名称
        :param series: 每个视频是否下载所有p，False时仅下载系列中的第一个视频
        :param image: 是否下载封面
        :param subtitle: 是否下载字幕
        :param dm: 是否下载弹幕
        :param only_audio: 是否仅下载音频
        :param codec:
        :return:
        """
        self._init_state(f'up:{url_or_mid}')
        ps = 30
        up_name, total_size, bv_ids = await api.get_up_video_info(self.client, url_or_mid, 1, ps, order, keyword)
        if self.hierarchy:
            path /= legal_title(f"【up】{up_name}")
            path.mkdir(parents=True, exist_ok=True)
        num = min(total_size, num)
        page_nums = num // ps + min(1, num % ps)
        cors = []
        for i in range(page_nums):
            if i + 1 == page_nums:
                p_num = num - (page_nums - 1) * ps
            else:
                p_num = ps
            cors.append(self._get_up_by_page(
                url_or_mid, path, i + 1, p_num, order, keyword, quality, series, image=image,
                subtitle=subtitle, dm=dm, only_audio=only_audio, codec=codec))
        await asyncio.gather(*cors)
        self._save_state()

    async def _get_up_by_page(self, url_or_mid, path: Path, pn=1, num=30, order='pubdate', keyword='', quality=0,
                              series=True, image=False, subtitle=False, dm=False, only_audio=False, codec='', ):
        ps = 30
        num = min(ps, num)
        _, _, bvids = await api.get_up_video_info(self.client, url_or_mid, pn, ps, order, keyword)
        bvids = bvids[:num]
        func = self.get_series if series else self.get_video
        # noinspection PyArgumentList
        await asyncio.gather(
            *[func(f'https://www.bilibili.com/video/{bv}', path=path, quality=quality, codec=codec,
                   image=image, subtitle=subtitle, dm=dm, only_audio=only_audio) for bv in bvids])

    async def get_series(self, url: str, path=Path('.'),
                         quality: Union[str, int] = 0, image=False, subtitle=False,
                         dm=False, only_audio=False, p_range: Sequence[int] = None, codec: str = ''):
        """
        下载某个系列（包括up发布的多p投稿，动画，电视剧，电影等）的所有视频。只有一个视频的情况下仍然可用该方法
        :cli: short: s
        :param url: 系列中任意一个视频的url
        :param path: 保存路径
        :param quality: 画面质量，0为可以观看的最高画质，越大质量越低，超过范围时自动选择最低画质，或者直接使用字符串指定'1080p'等名称
        :param image: 是否下载封面
        :param subtitle: 是否下载字幕
        :param dm: 是否下载弹幕
        :param only_audio: 是否仅下载音频
        :param p_range: 下载集数范围，例如(1, 3)：P1至P3
        :param codec: 视频编码（可通过info获取）
        :return:
        """
        standalone = self._state_path is None  # 被收藏夹/分区等批量入口调用时不重复初始化
        if standalone:
            self._init_state(f'series:{url}')
        try:
            async with self.api_sema:
                video_info = await api.get_video_info(self.client, url)
        except (APIResourceError, APIUnsupportedError) as e:
            # 系列入口即失效（视频已删除/地区限制等），记录报告供总结说明
            report = TaskReport(name=eclipse_str(url, 50),
                                video=f'失败: {str(e).split(" resource:")[0].strip()}')
            self.reports.append(report)
            return self.logger.warning(e) or report
        if self.hierarchy and len(video_info.pages) > 1:
            path /= video_info.title
            path.mkdir(parents=True, exist_ok=True)
        cors = [self.get_video(p.p_url, path=path,
                               quality=quality, image=image, subtitle=subtitle, dm=dm,
                               only_audio=only_audio, codec=codec,
                               video_info=video_info if idx == video_info.p else None)
                for idx, p in enumerate(video_info.pages)]
        if p_range:
            cors = cors_slice(cors, p_range)
        await asyncio.gather(*cors)
        if standalone:
            self._save_state()

    async def get_video(self, url: str, path=Path('.'),
                        quality: Union[str, int] = 0, image=False, subtitle=False, dm=False, only_audio=False,
                        codec: str = '', time_range: Tuple[int, int] = None, video_info: api.VideoInfo = None) -> TaskReport:
        """
        下载单个视频
        :cli: short: v
        :param url: 视频的url
        :param path: 保存路径
        :param quality: 画面质量，0为可以观看的最高画质，越大质量越低，超过范围时自动选择最低画质，或者直接使用字符串指定'1080p'等名称
        :param image: 是否下载封面
        :param subtitle: 是否下载字幕
        :param dm: 是否下载弹幕
        :param only_audio: 是否仅下载音频
        :param codec: 视频编码（可通过codec获取）
        :param time_range: 切片的时间范围
        :param video_info: 额外数据，提供时不用再次请求页面
        :return: TaskReport 本次任务的执行报告
        """
        report = TaskReport(name=eclipse_str(url, 50))
        self.reports.append(report)
        fp = {'q': str(quality), 'c': codec, 'oa': only_audio, 'af': self.audio_format,
              'img': image, 'sub': subtitle, 'dm': dm, 'sl': self.sub_lang}
        if self._state_path is None:  # 单视频直跑时懒初始化
            self._init_state(f'video:{url}')
        prev = self._prev_state.get(url)
        if prev and prev.get('video') == 'ok' and prev.get('fp') == fp \
                and not any(str(v).startswith('失败') for v in (prev.get('extra') or {}).values()) \
                and prev.get('file') and Path(prev['file']).exists():
            # 上次同参数下载成功且产物仍在：跳过（连API请求都不发起）
            report.video = '已存在'
            report.file = prev['file']
            self.logger.info(f'[green]上次已成功，跳过[/green] {Path(prev["file"]).name}')
            self._url_state[url] = {'video': 'ok', 'extra': {}, 'fp': fp, 'file': prev['file']}
            self._save_state()
            return report
        try:
            return await self._get_video_inner(url, path, quality, image, subtitle, dm, only_audio,
                                               codec, time_range, video_info, report)
        except Exception as e:  # 任何异常都转为报告记录，避免中断批量任务
            report.video = f'失败: {eclipse_str(str(e), 80)}'
            self.logger.error(f'{report.name} 视频下载失败: {eclipse_str(str(e), 120)}')
            return report
        finally:  # 状态写盘（原子替换），中断后下次可续
            if not report.video and (not report.file or not Path(report.file).exists()):
                # 任务被中断（Ctrl+C/取消）且主产物尚未落盘：如实记录为中断，重跑将续传
                report.video = '失败: 任务被中断（重跑将自动续传）'
            self._url_state[url] = {'video': report.video or 'ok', 'extra': report.extra,
                                    'fp': fp, 'file': report.file}
            try:
                self._save_state()  # 同步落盘，取消场景下也保证状态持久化
            except Exception:
                pass

    async def _get_video_inner(self, url: str, path, quality, image, subtitle, dm, only_audio,
                               codec, time_range, video_info, report: TaskReport) -> TaskReport:
        async with self.v_sema:
            if not video_info:
                try:
                    video_info = await api.get_video_info(self.client, url)
                except (APIResourceError, APIUnsupportedError) as e:
                    report.video = f'失败: {e}'
                    return self.logger.warning(e) or report
            p_name = legal_title(video_info.pages[video_info.p].p_name)
            task_name = legal_title(video_info.title, p_name)
            report.name = task_name
            # if title is too long, use p_name as base_name
            base_name = p_name if len(video_info.title) > self.title_overflow and self.hierarchy and p_name else \
                task_name
            media_name = base_name if not time_range else legal_title(base_name, *map(t2s, time_range))
            media_cors = []
            task_id = await self.progress.add_task(total=None, description=task_name)
            if video_info.dash:
                try:  # choose video quality
                    video, audio = video_info.dash.choose_quality(quality, codec)
                except KeyError:
                    report.video = '失败: 清晰度/编码不可用（可能需要大会员）'
                    self.logger.warning(
                        f"{task_name} 清晰度<{quality}> 编码<{codec}>不可用，请检查输入是否正确或是否需要大会员")
                else:
                    tmp: List[Tuple[api.Media, Path]] = []
                    # 1. only video
                    if not audio and not only_audio:
                        report.file = str(path / f'{media_name}.mp4')
                        tmp.append((video, path / f'{media_name}.mp4'))
                    # 2. video and audio
                    elif audio and not only_audio:
                        exists, media_path = path_check(path / f'{media_name}.mp4')
                        if exists:
                            report.video = '已存在'
                            report.file = str(media_path)
                            self.logger.info(f'[green]已存在[/green] {media_path.name}')
                        else:
                            tmp.append((video, path / f'{media_name}-v'))
                            tmp.append((audio, path / f'{media_name}-a'))
                            # task need to be merged
                            report.file = str(path / f'{media_name}.mp4')
                            await self.progress.update(task_id=task_id, upper=ffmpeg.combine)
                    # 3. only audio
                    elif audio and only_audio:
                        report.file = str(path / f'{media_name}{audio.suffix}')
                        tmp.append((audio, path / f'{media_name}{audio.suffix}'))
                    else:
                        report.video = '失败: 无音频流'
                        self.logger.warning(f"No audio for {task_name}")
                    # convert to coroutines
                    if not time_range:
                        media_cors.extend(self.get_file(t[0].urls, path=t[1], task_id=task_id) for t in tmp)
                    else:
                        if len(tmp) > 0:
                            fut = asyncio.Future()  # to fix key frame
                            v = tmp[0]
                            media_cors.append(self.get_media_clip(v[0].urls, v[1], time_range,
                                                                  init_range=v[0].segment_base['initialization'],
                                                                  seg_range=v[0].segment_base['index_range'],
                                                                  set_s=fut,
                                                                  task_id=task_id))
                        if len(tmp) > 1:  # with audio
                            a = tmp[1]
                            media_cors.append(self.get_media_clip(a[0].urls, a[1], time_range,
                                                                  init_range=a[0].segment_base['initialization'],
                                                                  seg_range=a[0].segment_base['index_range'],
                                                                  get_s=fut,
                                                                  task_id=task_id))

            elif video_info.other:
                self.logger.warning(
                    f"{task_name} 未解析到dash资源，转入durl mp4/flv下载（不需要会员的电影/番剧预览，不支持dash的视频）")
                media_name = base_name
                if len(video_info.other) == 1:
                    m = video_info.other[0]
                    media_cors.append(
                        self.get_file(m.urls, path=path / f'{media_name}.{m.suffix}', task_id=task_id))
                else:
                    exist, media_path = path_check(path / f'{media_name}.mp4')
                    if exist:
                        self.logger.info(f'[green]已存在[/green] {media_path.name}')
                    else:
                        p_sema = asyncio.Semaphore(self.part_concurrency)

                        async def _get_file(media: api.Media, p: Path) -> Path:
                            async with p_sema:
                                return await self.get_file(media.urls, path=p, task_id=task_id)

                        for i, m in enumerate(video_info.other):
                            f = f'{media_name}-{i}.{m.suffix}'
                            media_cors.append(_get_file(m, path / f))
                        await self.progress.update(task_id=task_id, upper=ffmpeg.concat)
            else:
                report.video = '失败: 需要大会员或该地区不支持'
                self.logger.warning(f'{task_name} 需要大会员或该地区不支持')
            # additional task
            add_cors = []
            add_labels = []
            if image or subtitle or dm:
                extra_path = path / "extra" if self.hierarchy else path
                extra_path.mkdir(exist_ok=True)
                if image:
                    add_cors.append(self.get_static(video_info.img_url, path=extra_path / base_name))
                    add_labels.append('封面')
                if subtitle:
                    add_cors.append(self.get_subtitle(url, path=extra_path, video_info=video_info))
                    add_labels.append('字幕')
                if dm:
                    try:
                        width, height = video.width, video.height
                    except UnboundLocalError:
                        width, height = 1920, 1080
                    add_cors.append(self.get_dm(
                        url, path=extra_path, convert_func=self._dm2ass_factory(width, height), video_info=video_info))
                    add_labels.append('弹幕')
            path_lst, extra_res = await asyncio.gather(asyncio.gather(*media_cors),
                                                       asyncio.gather(*add_cors, return_exceptions=True))
            # 附加产物结果上报（字幕/封面/弹幕失败明细）
            for label, res in zip(add_labels, extra_res):
                if isinstance(res, Exception):
                    report.extra[label] = f'失败: {eclipse_str(str(res), 60)}'
                    self.logger.warning(f'{task_name} {label}下载失败: {eclipse_str(str(res), 100)}')
                elif label == '字幕':
                    ok_paths, sub_fails = res
                    for sub_name, err in sub_fails:
                        report.extra[f'字幕({sub_name})'] = f'失败: {eclipse_str(str(err), 60)}'
            # 音频格式转换（-oa 且指定非aac格式时）：m4a无损重封装修复时长元数据 / mp3转码
            if only_audio and self.audio_format != 'aac' and path_lst:
                src = Path(path_lst[0])
                if src.exists():
                    target = src.with_suffix(f'.{self.audio_format}')
                    try:
                        await ffmpeg.convert_audio(src, target, fmt=self.audio_format)
                        report.file = str(target)
                        self.logger.info(f'[cyan]已完成转码[/cyan] {target.name}')
                    except Exception as e:
                        report.extra[f'音频转{self.audio_format}'] = f'失败: {eclipse_str(str(e), 60)}（保留原始AAC）'
                        self.logger.warning(
                            f'{task_name} 音频转{self.audio_format}失败，保留原始AAC文件: {eclipse_str(str(e), 100)}')

        if upper := self.progress.tasks[task_id].fields.get('upper', None):
            await upper(path_lst, media_path)
            self.logger.info(f'[cyan]已完成[/cyan] {media_path.name}')
        await self.progress.update(task_id, visible=False)
        return report

    @staticmethod
    def _dm2ass_factory(width: int, height: int):
        async def dm2ass(protobuf_bytes: bytes) -> bytes:
            loop = asyncio.get_event_loop()
            f = functools.partial(proto2ass, protobuf_bytes, width, height, font_size=width / 40, )
            content = await loop.run_in_executor(SingletonPPE(), f)
            return content.encode('utf-8')

        return dm2ass

    async def get_dm(self, url, path=Path('.'), update=False, convert_func=None, video_info=None):
        """
        下载视频的弹幕
        :cli: short: dm
        :param url: 视频url
        :param path: 保存路径
        :param update: 是否更新覆盖之前下载的弹幕文件
        :param convert_func:
        :param video_info: 额外数据，提供则不再访问前端
        :return:
        """
        if not video_info:
            video_info = await api.get_video_info(self.client, url)
        aid, cid = video_info.aid, video_info.cid
        file_type = '.' + ('pb' if not convert_func else convert_func.__name__.split('2')[-1])
        p_name = video_info.pages[video_info.p].p_name
        # to avoid file name too long bug
        if len(video_info.title) > self.title_overflow and self.hierarchy and p_name:
            file_name = legal_title(p_name, "弹幕") + file_type
        else:
            file_name = legal_title(video_info.title, p_name, "弹幕") + file_type
        file_path = path / file_name
        exist, file_path = path_check(file_path)
        if not update and exist:
            self.logger.info(f"[green]已存在[/green] {file_name}")
            return file_path
        dm_urls = await api.get_dm_urls(self.client, aid, cid)
        cors = [req_retry(self.client, dm_url) for dm_url in dm_urls]
        results = await asyncio.gather(*cors)
        content = b''.join(res.content for res in results)
        content = convert_func(content) if convert_func else content
        if asyncio.iscoroutine(content):
            content = await content
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(content)
        self.logger.info(f"[cyan]已完成[/cyan] {file_name}")
        return file_path

    async def get_subtitle(self, url, path=Path('.'), convert_func=json2srt, video_info=None, sub_lang: str = None):
        """
        下载视频的字幕文件
        :cli: short: sub
        :param url: 视频url
        :param path: 字幕文件保存路径
        :param convert_func: function used to convert original subtitle text
        :param video_info: 额外数据，提供则不再访问前端
        :param sub_lang: 字幕语言过滤，逗号分隔（如 '中文,en'），None时使用下载器配置
        :return:
        """
        if not video_info:
            video_info = await api.get_video_info(self.client, url)
        p, cid = video_info.p, video_info.cid
        p_name = video_info.pages[p].p_name
        langs = self.sub_lang if sub_lang is None else sub_lang
        try:
            subtitles = await api.get_subtitle_info(self.client, video_info.bvid, cid, langs=langs)
        except APIError as e:
            return self.logger.warning(e) or ([], [(f'{p_name}字幕', e)])
        cors = []
        names = []

        for sub_url, sub_name in subtitles:
            if len(video_info.title) > self.title_overflow and self.hierarchy and p_name:
                file_name = legal_title(p_name, sub_name)
            else:
                file_name = legal_title(video_info.title, p_name, sub_name)
            names.append(sub_name)
            cors.append(self.get_static(sub_url, path / file_name, convert_func=convert_func))
        # 逐轨捕获失败：字幕轨之间互不影响，失败信息供总结报告使用
        results = await asyncio.gather(*cors, return_exceptions=True)
        ok_paths = [p for p in results if not isinstance(p, Exception)]
        fails = [(n, r) for n, r in zip(names, results) if isinstance(r, Exception)]
        for sub_name, err in fails:
            self.logger.warning(f'字幕 {sub_name} 下载失败: {eclipse_str(str(err), 100)}')
        return ok_paths, fails

    def _init_state(self, scope: str):
        """载入历史下载状态（同一命令重跑时：跳过上次成功项，重试失败项）"""
        self._state_path = Path.home() / '.bilix' / 'state' / f"{hashlib.md5(scope.encode()).hexdigest()[:16]}.json"
        try:
            self._prev_state = json.loads(self._state_path.read_text(encoding='utf-8')).get('items', {})
            ok = sum(1 for v in self._prev_state.values() if v.get('video') == 'ok')
            self.logger.info(f'检测到上次下载状态: {len(self._prev_state)}项(成功{ok})，本次将跳过成功项并重试失败项')
        except (FileNotFoundError, json.JSONDecodeError):
            self._prev_state = {}

    def _save_state(self):
        """同步写盘（文件小，阻塞可忽略）：保证Ctrl+C/取消场景下状态也能落盘"""
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'items': self._url_state}
        tmp = self._state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self._state_path)

    def print_summary(self):
        """下载结束后输出总结：全部成功时仅一行，存在异常时列出明细（哪些视频/字幕没下载成功）"""
        if not self.reports:
            return
        from collections import Counter
        levels = Counter(r.level for r in self.reports)
        label = {'ok': '✅成功', 'exist': '⏭️已存在', 'partial': '⚠️部分失败', 'fail': '❌失败'}
        head = f'📋 下载总结: 共{len(self.reports)}项 | ' + ' '.join(
            f'{label[k]}{levels[k]}' for k in sorted(label, key=lambda k: -levels.get(k, 0)) if levels.get(k))
        if levels.get('fail') or levels.get('partial'):
            self.logger.warning(head)
            for r in self.reports:
                if r.level in ('fail', 'partial'):
                    self.logger.warning(f'  {label[r.level]} {r.name} — {r.detail()}')
        else:
            self.logger.info(head)
        if self._state_path and self._url_state:
            self.logger.info(f'状态已保存 {self._state_path}（重跑同一命令将自动跳过成功项）')

    @classmethod
    @auto_assemble
    def handle(cls, method: str, keys: Tuple[str, ...], options: dict):
        # get_cate无需url；get_up/get_favour支持裸id（文档承诺的行为）
        if cls.pattern.match(keys[0]) or method in {'cate', 'get_cate'} \
                or (method in {'up', 'get_up', 'fav', 'get_favour'} and keys[0].isdigit()):
            if method in {'auto', 'a'}:
                m = cls.parse_url(keys[0])
            elif method in cls._cli_map:
                m = cls._cli_map[method]
            else:
                raise HandleMethodError(cls, method=method)
            d = cls(sess_data=options['cookie'], **kwargs_filter(cls, options))
            return d, m
