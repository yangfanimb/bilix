import asyncio
import inspect
import logging
import re
import time
from functools import wraps
from typing import Union, Optional, Tuple
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import aiofiles
import httpx

from bilix.cli.assign import auto_assemble
from bilix.log import logger as dft_logger
from bilix.download.utils import req_retry, path_check
from dataclasses import dataclass, field
from typing import Optional
from bilix.progress.abc import Progress
from bilix.progress.cli_progress import CLIProgress
from bilix.exception import HandleMethodError
from pathlib import Path, PurePath

__all__ = ['BaseDownloader', 'TaskReport']


@dataclass
class TaskReport:
    """单个下载任务的执行报告，用于下载结束后的总结说明"""
    name: str
    video: str = ''  # ''=成功, '已存在', '失败:原因'
    extra: dict = field(default_factory=dict)  # 附加产物(字幕/弹幕/封面等) -> 状态或失败原因
    file: str = ''  # 主产物文件路径，用于跨运行跳过判断

    @property
    def level(self) -> str:
        if self.video.startswith('失败'):
            return 'fail'
        if any(v.startswith('失败') for v in self.extra.values()):
            return 'partial'
        if self.video == '已存在':
            return 'exist'
        return 'ok'

    def detail(self) -> str:
        parts = []
        if self.video.startswith('失败') or self.video == '已存在':
            parts.append(self.video)
        parts += [f'{k}{v}' for k, v in self.extra.items() if v.startswith('失败')]
        return '; '.join(parts)


class BaseDownloaderMeta(type):
    def __new__(cls, name, bases, dct):
        dct['_cli_info'] = {}
        dct['_cli_map'] = {}
        for method_name, method in dct.items():
            if not method_name.startswith('_') and asyncio.iscoroutinefunction(method):
                if 'path' in (sig := inspect.signature(method)).parameters:
                    dct[method_name] = cls.ensure_path(method, sig)

                if cls.check_unique_method(method, bases):
                    cli_info = cls.parse_cli_doc(method)
                    if cli_info:
                        dct['_cli_info'][method] = cli_info
                        dct['_cli_map'][method_name] = method
                        if cli_info['short']:
                            dct['_cli_map'][cli_info['short']] = method

        return super().__new__(cls, name, bases, dct)

    @staticmethod
    def check_unique_method(method_name: str, bases: Tuple[type, ...]):
        for base in bases:
            if method_name in base.__dict__:
                return False
        return True

    @staticmethod
    def parse_cli_doc(func) -> Optional[dict]:
        docstring = func.__doc__
        if not docstring or ':cli:' not in docstring:
            return
        params_matches = re.findall(r":param (\w+): (.+)", docstring)
        params = {param: description for param, description in params_matches}

        cli_short_match = re.search(r":cli: short: (\w+)", docstring)
        short_name = cli_short_match.group(1) if cli_short_match else None

        return {"short": short_name, "params": params}

    @staticmethod
    def ensure_path(func, sig):
        path_index = next(i for i, name in enumerate(sig.parameters) if name == 'path')

        @wraps(func)
        async def wrapper(*args, **kwargs):
            new_args = list(args)
            if path_index < len(args) and isinstance(args[path_index], str):
                new_args[path_index] = Path(args[path_index])
            elif 'path' in kwargs and isinstance(kwargs['path'], str):
                kwargs['path'] = Path(kwargs['path'])

            return await func(*new_args, **kwargs)

        wrapper.__annotations__['path'] = Union[Path, str]
        return wrapper


class BaseDownloader(metaclass=BaseDownloaderMeta):
    pattern: re.Pattern = None
    cookie_domain: str = ""
    _cli_info: dict
    _cli_map: dict

    def __init__(
            self,
            *,
            client: httpx.AsyncClient = None,
            browser: str = None,
            speed_limit: Union[float, int] = None,
            stream_retry: int = 5,
            progress: Progress = None,
            logger: logging.Logger = None,
            audio_format: str = 'aac',
    ):
        """

        :param client: client used for http request
        :param browser: load cookies from which browser
        :param speed_limit: global download rate for the downloader, should be a number (Byte/s unit)
        :param progress: progress obj
        :param audio_format: 仅下载音频(-oa)时的输出格式: aac/m4a/mp3
        """
        # use cli progress by default
        self.progress = progress or CLIProgress()
        self.logger = logger or dft_logger
        self.client = client if client else httpx.AsyncClient(headers={'user-agent': 'PostmanRuntime/7.29.0'})
        if browser:  # load cookies from browser
            self.update_cookies_from_browser(browser)
        assert speed_limit is None or speed_limit > 0
        self.speed_limit = speed_limit
        self.stream_retry = stream_retry
        # 仅下载音频(-oa)时的输出格式（单文件视频平台据此提取音轨；B站重载了整套逻辑）
        self.audio_format = (audio_format or 'aac').lower()
        # 下载任务执行报告收集器（供CLI结束时输出总结）
        self.reports: List[TaskReport] = []
        # active stream number
        self._stream_num = 0

    async def extract_audio(self, video_file: Path) -> Path:
        """从成品视频文件提取音轨（-oa，单文件视频平台通用后处理）。

        按self.audio_format转换: aac/m4a无损提取, mp3转码。转换失败时保留视频文件并抛出异常。
        :return: 音频文件路径
        """
        from bilix import ffmpeg as _ffmpeg
        video_file = Path(video_file)
        audio_file = video_file.with_suffix(f'.{self.audio_format}')
        try:
            await _ffmpeg.convert_audio(video_file, audio_file, fmt=self.audio_format)
            video_file.unlink(missing_ok=True)  # -oa语义：不保留视频
        except Exception as e:
            raise RuntimeError(f'音频提取({self.audio_format})失败，保留视频文件: {e}') from e
        return audio_file

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.__aexit__(exc_type, exc_val, exc_tb)

    async def aclose(self):
        """Close transport and proxies for httpx client"""
        await self.client.aclose()

    async def get_static(self, url: str, path: Union[str, Path], convert_func=None) -> Path:
        """

        :param url:
        :param path: file path without suffix
        :param convert_func: function used to convert http bytes content, must be named like ...2...
        :return: downloaded file path
        """
        # use suffix from convert_func's name
        if convert_func:
            suffix = '.' + convert_func.__name__.split('2')[-1]
        # try to find suffix from url
        else:
            suffix = PurePath(urlparse(url).path).suffix
        path = path.with_name(path.name + suffix)
        exist, path = path_check(path)
        if exist:
            self.logger.info(f'[green]已存在[/green] {path.name}')
            return path
        res = await req_retry(self.client, url)
        content = convert_func(res.content) if convert_func else res.content
        async with aiofiles.open(path, 'wb') as f:
            await f.write(content)
        self.logger.info(f'[cyan]已完成[/cyan] {path.name}')
        return path

    @asynccontextmanager
    async def _stream_context(self, times: int):
        """
        contextmanager to print log, slow down streaming and count active stream number

        :param times: error occur times which is related to sleep time
        :return:
        """
        self._stream_num += 1
        try:
            yield
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                self.logger.warning(f"STREAM slowing down since 403 forbidden {e}")
                await asyncio.sleep(10. * (times + 1))
            else:
                self.logger.warning(f"STREAM {e}")
                await asyncio.sleep(.5 * (times + 1))
            raise
        except httpx.TransportError as e:
            msg = f'STREAM {e.__class__.__name__} 异常可能由于网络条件不佳或并发数过大导致，若重复出现请考虑降低并发数'
            self.logger.warning(msg) if times > 2 else self.logger.debug(msg)
            await asyncio.sleep(.1 * (times + 1))
            raise
        except Exception as e:
            self.logger.warning(f'STREAM Unexpected Exception class:{e.__class__.__name__} {e}')
            raise
        finally:
            self._stream_num -= 1

    @property
    def stream_num(self):
        """current activate network stream number"""
        return self._stream_num

    @property
    def chunk_size(self) -> Optional[int]:
        if self.speed_limit and self.speed_limit < 1e5:  # 1e5 limit bound
            # only restrict chunk_size when speed_limit is too low
            return int(self.speed_limit * 0.1)  # 0.1 delay slope
        # default to None setup
        return None

    async def _check_speed(self, content_size):
        if self.speed_limit and (cur_speed := self.progress.active_speed) > self.speed_limit:
            t_tgt = content_size / self.speed_limit * self.stream_num
            t_real = content_size / cur_speed
            t = t_tgt - t_real
            await asyncio.sleep(t)

    def report(self, name: str, video: str = '', extra: dict = None, file: str = '') -> TaskReport:
        """记录一个任务报告（供站点子类上报成败，CLI结束统一输出总结）"""
        r = TaskReport(name=name, video=video, extra=extra or {}, file=file)
        self.reports.append(r)
        if r.video.startswith('失败') or any(v.startswith('失败') for v in r.extra.values()):
            self.logger.warning(f'{r.name} — {r.detail()}')
        return r

    def print_summary(self):
        """下载结束后输出总结：全部成功时仅一行，存在异常时列出明细"""
        from collections import Counter
        if not self.reports:
            return
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

    def update_cookies_from_browser(self, browser: str):
        try:
            a = time.time()
            import browser_cookie3
            f = getattr(browser_cookie3, browser.lower())
            self.logger.debug(f"trying to load cookies from {browser}: {self.cookie_domain}, may need auth")
            self.client.cookies.update(f(domain_name=self.cookie_domain))
            self.logger.debug(f"load complete, consumed time: {time.time() - a} s")
        except AttributeError:
            raise AttributeError(f"Invalid Browser {browser}")

    @classmethod
    def _decide_handle(cls, method: str, keys: Tuple[str, ...], options: dict) -> bool:
        """check if the cls can be handled by this downloader"""
        if cls.pattern:
            return cls.pattern.match(keys[0]) is not None
        else:
            return method in cls._cli_map

    @classmethod
    @auto_assemble
    def handle(cls, method: str, keys: Tuple[str, ...], options: dict):
        if cls._decide_handle(method, keys, options):
            if method in {'auto', 'a'}:
                # url已按pattern匹配到本站点：自动选择主下载方法（优先get_video）
                if 'get_video' in cls._cli_map:
                    method = 'get_video'
                elif 'get_series' in cls._cli_map:
                    method = 'get_series'
                else:
                    method = next(iter(cls._cli_map))
            try:
                method = cls._cli_map[method]
            except KeyError:
                raise HandleMethodError(cls, method)
            return cls, method
