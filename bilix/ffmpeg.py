"""
just some useful ffmpeg commands wrapped in python
"""
import os
import shutil
from anyio import run_process
from typing import List
from pathlib import Path
import tempfile

_ffmpeg_with_lame = None  # 缓存带libmp3lame的ffmpeg二进制路径


async def _ffmpeg_with_mp3_encoder() -> str:
    """探测支持libmp3lame的ffmpeg二进制（不同构建差异大），找不到时抛出带行动建议的异常"""
    global _ffmpeg_with_lame
    if _ffmpeg_with_lame:
        return _ffmpeg_with_lame
    candidates = [os.environ.get('FFMPEG_PATH') or 'ffmpeg', '/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg']
    tried = []
    for cand in candidates:
        path = shutil.which(cand) or cand
        if path in tried or not os.path.exists(path):
            continue
        tried.append(path)
        try:
            res = await run_process([path, '-hide_banner', '-encoders'])
            if 'libmp3lame' in res.stdout.decode(errors='ignore'):
                _ffmpeg_with_lame = path
                return path
        except Exception:
            continue
    raise ValueError(f'环境中的ffmpeg均不带libmp3lame编码器（已尝试: {tried}），'
                     f'无法转码mp3。可安装带libmp3lame的ffmpeg，或改用 --audio-format m4a（无损重封装）')


async def concat(path_lst: List[Path], output_path: Path, remove=True):
    with tempfile.NamedTemporaryFile('w', dir=output_path.parent, delete=False) as fp:
        for path in path_lst:
            fp.write(f"file '{path.name}'\n")
        tmp_out = output_path.with_name(f'{output_path.stem}.tmp{output_path.suffix}')  # 原子写：保留扩展名
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', fp.name, '-c', 'copy', '-loglevel', 'quiet',
               str(tmp_out)]
    await run_process(cmd)
    os.replace(tmp_out, output_path)
    os.remove(fp.name)
    if remove:
        for path in path_lst:
            os.remove(path)


async def convert_audio(input_path: Path, output_path: Path, fmt: str = 'm4a', remove=True):
    """音频格式转换/重封装（-oa 配合 --audio-format 使用）

    :param fmt: aac=提取ADTS流（.aac）；m4a=无损重封装（不依赖编码器，修复ADTS流时长元数据不准的问题）；
                mp3=有损转码（自动探测带libmp3lame的ffmpeg）。输入可为视频容器（-vn忽略视频流）
    """
    tmp_out = output_path.with_name(f'{output_path.stem}.tmp{output_path.suffix}')  # 原子写：保留扩展名
    if fmt == 'aac':  # 从任意容器提取音轨为ADTS
        cmd = ['ffmpeg', '-y', '-i', str(input_path), '-vn', '-c:a', 'copy', '-f', 'adts', str(tmp_out)]
    elif fmt == 'm4a':
        cmd = ['ffmpeg', '-y', '-i', str(input_path), '-vn', '-c:a', 'copy', str(tmp_out)]
    elif fmt == 'mp3':
        binary = await _ffmpeg_with_mp3_encoder()
        cmd = [binary, '-y', '-i', str(input_path), '-vn', '-codec:a', 'libmp3lame', '-q:a', '2',
               str(tmp_out)]
    else:
        raise ValueError(f'unsupported audio format: {fmt}')
    await run_process(cmd)
    os.replace(tmp_out, output_path)
    if remove:
        os.remove(input_path)


async def combine(path_lst: List[Path], output_path: Path, remove=True):
    tmp_out = output_path.with_name(f'{output_path.stem}.tmp{output_path.suffix}')  # 原子写：保留扩展名
    cmd = ['ffmpeg', '-y']
    for path in path_lst:
        cmd.extend(['-i', str(path)])
    # for flac, use -strict -2
    cmd.extend(['-c', 'copy', '-strict', '-2', '-loglevel', 'quiet', str(tmp_out)])
    await run_process(cmd)
    os.replace(tmp_out, output_path)
    if remove:
        for path in path_lst:
            os.remove(path)


async def time_range_clip(input_path: Path, start: int, t: int, output_path: Path, remove=True):
    # for flac, use -strict -2
    tmp_out = output_path.with_name(f'{output_path.stem}.tmp{output_path.suffix}')  # 原子写：保留扩展名
    cmd = ['ffmpeg', '-y', '-ss', f'{start:.1f}', '-t', f'{t:.1f}', '-i', str(input_path), '-codec', 'copy', '-strict', '-2',
           '-loglevel', 'quiet', '-f', 'mp4', str(tmp_out)]
    await run_process(cmd)
    os.replace(tmp_out, output_path)
    if remove:
        os.remove(input_path)
