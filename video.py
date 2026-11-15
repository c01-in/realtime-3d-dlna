"""Render a selected NAS video for 1080p top-and-bottom 3D displays."""

import json
import functools
import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import numpy as np
from offline.two_dvr_gpu import GpuStereoRenderer
from offline.two_dvr_render import DEFAULT_FLAT_FOV_DEG, effective_eye_distance_mm
from utils.subprocess_hidden import hidden_subprocess_kwargs

STEREO_STRENGTH = 0.5
HOLE_FILL = 'soft_shift'
MIME = 'video/MP2T'
CONTENT_FEATURES = 'DLNA.ORG_OP=01;DLNA.ORG_CI=1'
log = logging.getLogger('live_sbs')


@functools.lru_cache(maxsize=64)
def probe(source, revision):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)],
        capture_output=True, check=True, timeout=12, **hidden_subprocess_kwargs()
    )
    info = json.loads(result.stdout)
    video = next((stream for stream in info['streams']
                  if stream['codec_type'] == 'video' and not stream.get('disposition', {}).get('attached_pic')), None)
    if video is None:
        raise ValueError('No video stream')
    width, height = video['width'], video['height']
    rotation = next((s.get('rotation', 0) for s in video.get('side_data_list', []) if 'rotation' in s), 0)
    sar = video.get('sample_aspect_ratio', '1:1')
    if sar not in ('N/A', '0:1'):
        width = round(width * Fraction(sar.replace(':', '/')))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    scale = min(1, 1920 / width, 1080 / height)
    width, height = [max(2, round(side * scale / 2) * 2) for side in (width, height)]
    fps = next((Fraction(video[k]) for k in ('avg_frame_rate', 'r_frame_rate')
                if video.get(k) not in (None, '0/0', '0/1', 'N/A')), Fraction(25))
    hdr = video.get('color_transfer') in {'smpte2084', 'arib-std-b67'}
    duration = next((Fraction(value) for value in (video.get('duration'), info['format'].get('duration'))
                     if value and value != 'N/A' and Fraction(value) > 0), None)
    if duration is None:
        raise ValueError('Video duration unavailable')
    cuda_format = {'yuv420p': 'nv12', 'nv12': 'nv12', 'yuv420p10le': 'p010le', 'p010le': 'p010le'}.get(video.get('pix_fmt'))
    if rotation or video.get('codec_name') not in {'hevc', 'h264', 'av1', 'vp9'}:
        cuda_format = None
    return video['index'], width, height, fps, hdr, duration, cuda_format


def top_bottom_filter(width, height):
    scale = min(1920 / width, 1080 / height)
    eye_width = min(1920, max(4, round(width * scale / 4) * 4))
    eye_height = min(540, max(4, round(height * scale / 8) * 4))
    eye = f'scale={eye_width}:{eye_height}:flags=lanczos,pad=1920:540:(ow-iw)/2:(oh-ih)/2'
    return (f'split[left][right];[left]crop={width}:{height}:0:0,{eye}[top];'
            f'[right]crop={width}:{height}:{width}:0,{eye}[bottom];'
            '[top][bottom]vstack,setsar=1,scale=out_color_matrix=bt709:out_range=tv')


class TopBottomStream:
    def __init__(self, source, start_frame, video, frames, predict, lead_in):
        index, self.width, self.height, fps, hdr, _, cuda_format = video
        self.predict = predict
        self.lead_in = lead_in
        start = start_frame / fps
        decode_start = (start_frame - self.lead_in) / fps
        self.started = time.perf_counter()
        self.frames = 0
        self.decode_seconds = 0.0
        self.depth_seconds = 0.0
        self.render_seconds = 0.0
        self.stop = threading.Event()
        self.error = None
        seek = ['-ss', f'{float(start):.9f}'] if start else []
        decode_seek = ['-ss', f'{float(decode_start):.9f}'] if decode_start else []
        scale = f'scale={self.width}:{self.height}:flags=lanczos'
        tone_map = (
            'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
            'tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=full,'
        ) if hdr else ''
        common = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
        limit = ['-t', f'{float(frames / fps):.9f}']
        ffmpeg_stderr = sys.stderr

        def start_decode(pixel_format):
            if pixel_format:
                hardware = ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
                resize = (f'scale_cuda={self.width}:{self.height}:interp_algo=lanczos:format={pixel_format},'
                          f'hwdownload,format={pixel_format}')
            else:
                hardware, resize = [], scale
            filters = f'{resize},fps={fps},setsar=1,{tone_map}format=rgb24'
            return subprocess.Popen(
                [*common, *decode_seek, *hardware, '-i', str(source), '-map', f'0:{index}', '-an', '-sn', '-vf', filters,
                 '-frames:v', str(frames + self.lead_in), '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=ffmpeg_stderr,
                **hidden_subprocess_kwargs()
            )

        self.start_decode = start_decode
        self.cuda_format = cuda_format
        self.decode = start_decode(cuda_format)
        try:
            self.encode = subprocess.Popen(
                [*common, '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{self.width * 2}x{self.height}',
                 '-r', str(fps), '-i', 'pipe:0', *seek, '-i', str(source),
                 '-map', '0:v:0', '-map', '1:a:0?', '-c:a', 'aac', '-b:a', '192k',
                 '-af', 'aresample=48000,asetpts=PTS+1024/SR/TB',
                 '-vf', top_bottom_filter(self.width, self.height), '-c:v', 'hevc_nvenc',
                 '-preset', 'p4', '-tune', 'll', '-bf', '0', '-g', str(max(1, round(fps))),
                 '-b:v', '20M', '-maxrate', '24M', '-bufsize', '4M',
                 '-pix_fmt', 'yuv420p', '-color_range', 'tv', '-colorspace', 'bt709',
                 '-color_trc', 'bt709', '-color_primaries', 'bt709', *limit,
                 '-output_ts_offset', f'{float(start):.9f}', '-mpegts_copyts', '1', '-avoid_negative_ts', 'disabled',
                 '-mpegts_flags', '+resend_headers+initial_discontinuity', '-muxdelay', '0', '-muxpreload', '0',
                 '-flush_packets', '1', '-f', 'mpegts', 'pipe:1'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=ffmpeg_stderr,
                **hidden_subprocess_kwargs()
            )
        except BaseException:
            self.decode.kill()
            self.decode.wait()
            self.decode.stdout.close()
            raise
        self.worker = threading.Thread(target=self.convert, name='hdr-sbs', daemon=True)
        self.worker.start()

    def convert(self):
        frame_bytes = self.width * self.height * 3
        decoded = 0

        def read_depth():
            nonlocal decoded
            while not self.stop.is_set():
                started = time.perf_counter()
                raw = self.decode.stdout.read(frame_bytes)
                self.decode_seconds += time.perf_counter() - started
                if not raw:
                    result = self.decode.wait()
                    if result and self.cuda_format and not decoded and not self.stop.is_set():
                        log.warning('NVDEC could not start for this segment, retrying in software')
                        self.decode.stdout.close()
                        self.cuda_format = None
                        self.decode = self.start_decode(None)
                        continue
                    if result:
                        raise RuntimeError('FFmpeg could not decode the source')
                    return None
                if len(raw) != frame_bytes:
                    raise RuntimeError('FFmpeg returned an incomplete frame')
                rgb = np.frombuffer(raw, np.uint8).reshape(self.height, self.width, 3)
                started = time.perf_counter()
                depth = self.predict(rgb)
                self.depth_seconds += time.perf_counter() - started
                decoded += 1
                if decoded > self.lead_in:
                    return rgb, depth
            return None

        try:
            renderer = GpuStereoRenderer(
                self.width, self.height, 'flat3d', effective_eye_distance_mm(65, STEREO_STRENGTH),
                HOLE_FILL, DEFAULT_FLAT_FOV_DEG
            )
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='3d-depth') as depth_worker:
                pending = depth_worker.submit(read_depth)
                while not self.stop.is_set() and (item := pending.result()) is not None:
                    pending = depth_worker.submit(read_depth)
                    rgb, depth = item
                    started = time.perf_counter()
                    packed = renderer.render(rgb, depth)
                    self.encode.stdin.write(memoryview(packed).cast('B'))
                    self.render_seconds += time.perf_counter() - started
                    self.frames += 1
                    if self.frames == 1:
                        log.info('First oVDA top-bottom frame in %.2fs', time.perf_counter() - self.started)
        except Exception as exc:
            if not self.stop.is_set():
                self.error = exc
                log.exception('Top-bottom conversion failed')
        finally:
            try:
                self.encode.stdin.close()
            except OSError:
                pass

    def read(self):
        return self.encode.stdout.read1(65536)

    def close(self):
        self.stop.set()
        for process in (self.decode, self.encode):
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()
        self.worker.join(timeout=3)
        self.decode.stdout.close()
        self.encode.stdout.close()
        log.info('Top-bottom segment closed, %d frames in %.2fs, decode=%s', self.frames,
                 time.perf_counter() - self.started, 'NVDEC' if self.cuda_format else 'CPU')
        log.debug('Stage time decode=%.2fs, depth=%.2fs, render+pipe=%.2fs, lead-in=%d',
                  self.decode_seconds, self.depth_seconds, self.render_seconds, self.lead_in)
