import re
import asyncio
from pathlib import Path
from typing import Union
import httpx
from . import api
from bilix.download.base_downloader_m3u8 import BaseDownloaderM3u8
from bilix.download.base_downloader_part import BaseDownloaderPart
from bilix.download.utils import eclipse_str
from bilix import ffmpeg


class DownloaderYoutube(BaseDownloaderM3u8, BaseDownloaderPart):
    """多继承：get_file(直链) + get_m3u8_video(HLS分轨) 两种下载管线"""

    pattern = re.compile(r"^https?://([A-Za-z0-9-]+\.)*(youtube\.com|youtu\.be)")

    def __init__(
            self,
            *,
            client: httpx.AsyncClient = None,
            browser: str = None,
            speed_limit: Union[float, int] = None,
            stream_retry: int = 5,
            progress=None,
            logger=None,
            part_concurrency: int = 10,
            # unique params
            video_concurrency: Union[int, asyncio.Semaphore] = 3,
            audio_format: str = 'aac',
    ):
        client = client or httpx.AsyncClient(**api.dft_client_settings)
        super().__init__(
            client=client,
            browser=browser,
            speed_limit=speed_limit,
            stream_retry=stream_retry,
            progress=progress,
            logger=logger,
            part_concurrency=part_concurrency,
            video_concurrency=video_concurrency,
            audio_format=audio_format,
        )

    async def get_video(self, url: str, path=Path('.'), quality=0, subtitle=False, sub_lang: str = None,
                        only_audio=False):
        """
        下载单个视频
        :cli: short: v
        :param url:
        :param path:
        :param quality: 画质选择，0=最高；'1080p'等字符串=高度≤目标的最大者（HLS路径生效）
        :param subtitle: 是否下载字幕（YouTube CC字幕，json3转SRT）
        :param sub_lang: 字幕语言过滤，逗号分隔（如 'en,zh'），None时使用下载器配置
        :param only_audio: 仅下载音频
        :return:
        """
        async with self.v_sema:
            video_info = await api.get_video_info(self.client, url, quality=quality)
            video_path = path / (video_info.title + '.mp4')
            if not only_audio and video_path.exists():
                return self.logger.info(f'[green]已存在[/green] {video_path.name}')
            task_id = await self.progress.add_task(description=video_info.title, upper=True)
            add_cors = []
            if subtitle:
                add_cors.append(self.get_subtitle(url, path=path, sub_lang=sub_lang))
            if only_audio:
                # 仅音频：HLS分轨只下载音频清单；muxed/直链则从成品视频提取
                if video_info.hls and video_info.audio_url:
                    audio_file = await self.get_m3u8_video(m3u8_url=video_info.audio_url,
                                                           path=path / (video_info.title + '-a.mp4'))
                elif video_info.audio_url:
                    audio_file = await self.get_file(url_or_urls=video_info.audio_url,
                                                     path=path / (video_info.title + '-a.mp4'), task_id=task_id)
                else:
                    audio_file = await self.get_file(url_or_urls=video_info.video_url,
                                                     path=path / (video_info.title + '-a.mp4'), task_id=task_id)
                await self.progress.update(task_id, visible=False)
                audio_out = await self.extract_audio(Path(audio_file))
                self.logger.info(f'[cyan]已完成[/cyan] {audio_out.name}')
                return audio_out
            if video_info.hls:
                # HLS分轨：分别下载视频/音频清单（m3u8管线自带分段下载与解密）
                # 中间文件必须带.mp4后缀：ffmpeg.concat需按扩展名推断输出容器格式
                path_lst = await asyncio.gather(
                    self.get_m3u8_video(m3u8_url=video_info.video_url, path=path / (video_info.title + '-v.mp4')),
                    self.get_m3u8_video(m3u8_url=video_info.audio_url, path=path / (video_info.title + '-a.mp4')),
                )
            elif video_info.audio_url:
                path_lst = await asyncio.gather(
                    self.get_file(url_or_urls=video_info.video_url, path=path / (video_info.title + '-v'), task_id=task_id),
                    self.get_file(url_or_urls=video_info.audio_url, path=path / (video_info.title + '-a'), task_id=task_id)
                )
            else:
                # muxed 单文件（音视频合一），无需合并
                part = await self.get_file(url_or_urls=video_info.video_url,
                                           path=path / (video_info.title + '-v'), task_id=task_id)
                await self.progress.update(task_id, visible=False)
                part.rename(video_path)
                self.logger.info(f'[cyan]已完成[/cyan] {video_path.name}')
                return video_path
        if add_cors:
            await asyncio.gather(*add_cors, return_exceptions=True)
        await ffmpeg.combine(path_lst, output_path=video_path)
        self.logger.info(f'[cyan]已完成[/cyan] {video_path.name}')
        await self.progress.update(task_id, visible=False)
        return video_path

    async def get_subtitle(self, url: str, path=Path('.'), sub_lang: str = None, convert_func=None):
        """
        下载YouTube字幕（CC字幕，json3转SRT）
        :cli: short: sub
        :param url: 视频url
        :param path: 保存路径
        :param sub_lang: 语言过滤，逗号分隔（如 'en,zh'），None时使用下载器配置
        :param convert_func: 未提供时使用内置json3→SRT转换
        :return:
        """
        def xml2srt(content: bytes) -> bytes:  # 函数名约定: '2'后缀决定产物扩展名
            return api.convert_subtitle(content)

        video_id = api._parse_video_id(url)
        langs = self.sub_lang if sub_lang is None else sub_lang
        try:
            tracks = await api.get_captions(self.client, video_id, langs=langs)
        except Exception as e:
            return self.logger.warning(f'YouTube字幕获取失败: {e}')
        if not tracks:
            return self.logger.warning('该视频没有可用CC字幕（AI字幕需在YouTube网页端查看）')
        results = await asyncio.gather(
            *[self.get_static(track_url, path / f'{video_id}-{name}', convert_func=convert_func or xml2srt)
              for track_url, name in tracks], return_exceptions=True)
        for (_, name), res in zip(tracks, results):
            if isinstance(res, Exception):
                self.logger.warning(f'字幕 {name} 下载失败: {eclipse_str(str(res), 100)}')
        return [r for r in results if not isinstance(r, Exception)]
