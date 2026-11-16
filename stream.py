"""A byte-addressable MPEG-TS representation generated only as requested."""

import asyncio
from collections import OrderedDict
import logging
import re
import threading
from fractions import Fraction

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from video import CONTENT_FEATURES, MIME, TopBottomStream

PACKET_SIZE = 188
NULL_PACKET = b'\x47\x1f\xff\x10' + b'\xff' * 184
SLOT_RATE = 32_000_000
log = logging.getLogger(__name__)
recent = OrderedDict()
recent_lock = threading.Lock()
render_locks = tuple(threading.Lock() for _ in range(64))


def size_for(frames, fps):
    return round(Fraction(SLOT_RATE * frames, 8 * PACKET_SIZE) / fps) * PACKET_SIZE


class SeekableVideo:
    def __init__(self, source, video, revision, worker):
        self.source = source
        self.video = video
        self.revision = revision
        self.worker = worker
        self.connection = None
        _, _, _, fps, _, duration, _ = video
        slot_frames = max(1, round(4 * fps))
        self.slot_size = size_for(slot_frames, fps)
        complete, last = divmod(max(1, round(duration * fps)), slot_frames)
        self.size = complete * self.slot_size + size_for(last, fps)

    def render(self, slot, cancelled):
        key = self.source, self.revision, slot
        lock = render_locks[hash(key) % len(render_locks)]
        while not cancelled.is_set():
            if lock.acquire(timeout=0.1):
                try:
                    return self.cached_segment(key, slot, cancelled)
                finally:
                    lock.release()
        return b''

    def cached_segment(self, key, slot, cancelled):
        with recent_lock:
            if key in recent:
                recent.move_to_end(key)
                return recent[key]
        if not cancelled.is_set():
            segment = self.render_segment(slot, cancelled)
            if segment and not cancelled.is_set():
                with recent_lock:
                    recent[key] = segment
                    recent.move_to_end(key)
                    if len(recent) > 3:
                        recent.popitem(last=False)
                return segment
        return b''

    def render_segment(self, slot, cancelled):
        try:
            if self.connection is None:
                self.connection = self.worker.connect()
                self.connection.send((self.source, self.video))
            if cancelled.is_set():
                return b''
            self.connection.send(slot)
            while not self.connection.poll(0.1):
                if cancelled.is_set():
                    self.connection.send(None)
                    break
            data = self.connection.recv_bytes()
        except EOFError as exc:
            raise RuntimeError('GPU worker disconnected') from exc
        if data.startswith(b'\0'):
            raise RuntimeError(data[1:].decode('utf-8'))
        return data

    def close(self):
        if self.connection is not None:
            self.connection.close()


class SegmentRenderer:
    def __init__(self, source, video, engine):
        self.source = source
        self.video = video
        self.engine = engine
        self.predict = None
        self.next_frame = None
        fps, duration = video[3], video[5]
        self.frames = max(1, round(duration * fps))
        self.slot_frames = max(1, round(4 * fps))
        self.fps = fps

    def render(self, slot, is_cancelled):
        if is_cancelled():
            return b''
        start = slot * self.slot_frames
        frames = min(self.slot_frames, self.frames - start)
        capacity = size_for(frames, self.fps)
        lead_in = 0
        if self.next_frame != start:
            _, width, height, *_ = self.video
            self.predict = self.engine.sequence(width, height)
            lead_in = min(start, self.engine.history)
        self.next_frame = None
        stream = TopBottomStream(self.source, start, self.video, frames, self.predict, lead_in)
        try:
            data = bytearray()
            while not is_cancelled() and (chunk := stream.read()):
                data.extend(chunk)
                if len(data) > capacity:
                    raise RuntimeError(f'Encoded TS segment exceeded its byte budget ({len(data)} > {capacity})')
            if is_cancelled():
                return b''
            if stream.error or stream.encode.wait(timeout=4) or not data or len(data) % PACKET_SIZE:
                raise RuntimeError('Failed to produce a complete MPEG-TS segment') from stream.error
            if stream.frames == frames:
                self.next_frame = start + frames
            data.extend(NULL_PACKET * ((capacity - len(data)) // PACKET_SIZE))
            return bytes(data)
        finally:
            stream.close()


def byte_range(value, total):
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip(), re.IGNORECASE)
    if match is None or not any(match.groups()):
        return None
    first, last = match.groups()
    if first:
        start, end = int(first), min(total - 1, int(last)) if last else total - 1
    else:
        start, end = max(0, total - int(last)), total - 1
    return (start, end) if start <= end else None


def make_response(request, asset, resources):
    range_header = request.headers.get('range')
    headers = {'Accept-Ranges': 'bytes', 'contentFeatures.dlna.org': CONTENT_FEATURES,
               'transferMode.dlna.org': 'Streaming'}
    if range_header:
        selected = byte_range(range_header, asset.size)
        if selected is None:
            return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{asset.size}'})
        start, end = selected
        headers['Content-Range'] = f'bytes {start}-{end}/{asset.size}'
        status = 206
    else:
        start, end, status = 0, asset.size - 1, 200
    headers['Content-Length'] = str(end - start + 1)
    if request.method == 'HEAD':
        return Response(status_code=status, headers=headers, media_type=MIME)

    return PlaybackResponse(request, asset, resources, start, end, status_code=status,
                            headers=headers, media_type=MIME)


class PlaybackResponse(StreamingResponse):
    def __init__(self, request, asset, resources, start, end, **kwargs):
        self.request = request
        self.asset = asset
        self.resources = resources
        self.first, self.offset = divmod(start, asset.slot_size)
        self.last, self.end_offset = divmod(end, asset.slot_size)
        self.cancelled = threading.Event()
        self.pending = None
        super().__init__(self.chunks(), **kwargs)

    def queue(self, slot):
        self.pending = asyncio.create_task(asyncio.to_thread(self.asset.render, slot, self.cancelled))

    async def __call__(self, scope, receive, send):
        self.resources.begin()
        try:
            self.queue(self.first)
            try:
                while not self.pending.done():
                    await asyncio.wait({self.pending}, timeout=0.25)
                    if await self.request.is_disconnected():
                        raise HTTPException(499, 'Player disconnected')
                self.pending.result()
            except (OSError, RuntimeError, ValueError) as exc:
                log.exception('Could not produce top-bottom segment for %s', self.asset.source.name)
                raise HTTPException(502, '3D conversion could not produce the requested bytes') from exc
            await super().__call__(scope, receive, send)
        finally:
            self.cancelled.set()
            # Keep the request active until any prefetched conversion has stopped.
            if self.pending is not None and not self.pending.done():
                self.pending.add_done_callback(self.release)
            else:
                self.release(self.pending)

    def release(self, task):
        if task is not None and not task.cancelled():
            task.exception()
        try:
            self.asset.close()
        finally:
            self.resources.end()

    async def chunks(self):
        for slot in range(self.first, self.last + 1):
            data = await asyncio.shield(self.pending)
            if slot < self.last:
                self.queue(slot + 1)
            begin = self.offset if slot == self.first else 0
            stop = self.end_offset + 1 if slot == self.last else len(data)
            for cursor in range(begin, stop, 65536):
                yield data[cursor:min(cursor + 65536, stop)]
