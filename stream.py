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


class SeekableVideo:
    def __init__(self, source, video, revision, engine):
        self.source = source
        self.video = video
        self.revision = revision
        self.engine = engine
        self.predict = None
        self.next_frame = None
        _, _, _, self.fps, _, duration, _ = video
        self.frames = max(1, round(duration * self.fps))
        self.slot_frames = max(1, round(4 * self.fps))
        self.slot_size = self.size_for(self.slot_frames)
        complete, last = divmod(self.frames, self.slot_frames)
        self.size = complete * self.slot_size + self.size_for(last)

    def size_for(self, frames):
        return round(Fraction(SLOT_RATE * frames, 8 * PACKET_SIZE) / self.fps) * PACKET_SIZE

    def render(self, slot, cancelled):
        key = self.source, self.revision, slot
        lock = render_locks[hash(key) % len(render_locks)]
        while not cancelled.is_set():
            if lock.acquire(timeout=0.1):
                try:
                    return self.render_segment(key, slot, cancelled)
                finally:
                    lock.release()
        return b''

    def render_segment(self, key, slot, cancelled):
        with recent_lock:
            if key in recent:
                recent.move_to_end(key)
                return recent[key]
        if cancelled.is_set():
            return b''
        start = slot * self.slot_frames
        frames = min(self.slot_frames, self.frames - start)
        capacity = self.size_for(frames)
        lead_in = 0
        if self.next_frame != start:
            _, width, height, *_ = self.video
            self.predict = self.engine.sequence(width, height)
            lead_in = min(start, self.engine.history)
        self.next_frame = None
        stream = TopBottomStream(self.source, start, self.video, frames, self.predict, lead_in)
        if cancelled.is_set():
            stream.close()
            return b''
        try:
            data = bytearray()
            while not cancelled.is_set() and (chunk := stream.read()):
                data.extend(chunk)
                if len(data) > capacity:
                    raise RuntimeError(f'Encoded TS segment exceeded its byte budget ({len(data)} > {capacity})')
            if cancelled.is_set():
                return b''
            if stream.error or stream.encode.wait(timeout=4) or not data or len(data) % PACKET_SIZE:
                raise RuntimeError('Failed to produce a complete MPEG-TS segment') from stream.error
            if stream.frames == frames:
                self.next_frame = start + frames
            data.extend(NULL_PACKET * ((capacity - len(data)) // PACKET_SIZE))
            segment = bytes(data)
            with recent_lock:
                recent[key] = segment
                recent.move_to_end(key)
                if len(recent) > 3:
                    recent.popitem(last=False)
            return segment
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


async def seekable_response(request, asset):
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

    first, offset = divmod(start, asset.slot_size)
    last = end // asset.slot_size
    cancelled = threading.Event()

    def render(slot):
        return asset.render(slot, cancelled)

    task = asyncio.create_task(asyncio.to_thread(render, first))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.25)
            if await request.is_disconnected():
                raise HTTPException(499, 'Player disconnected')
        initial = task.result()
    except (OSError, RuntimeError, ValueError) as exc:
        cancelled.set()
        log.exception('Could not produce top-bottom segment for %s', asset.source.name)
        raise HTTPException(502, '3D conversion could not produce the requested bytes') from exc
    except BaseException:
        cancelled.set()
        raise

    async def chunks():
        pending = None
        try:
            for slot in range(first, last + 1):
                if await request.is_disconnected():
                    break
                data = initial if slot == first else await pending
                pending = asyncio.create_task(asyncio.to_thread(render, slot + 1)) if slot < last else None
                begin = offset if slot == first else 0
                stop = end - slot * asset.slot_size + 1 if slot == last else len(data)
                for cursor in range(begin, stop, 65536):
                    yield bytes(data[cursor:min(cursor + 65536, stop)])
        finally:
            cancelled.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)

    return StreamingResponse(chunks(), status_code=status, headers=headers, media_type=MIME)
