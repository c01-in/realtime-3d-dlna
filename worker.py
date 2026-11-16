"""Manage the GPU process and its lifetime across playback requests."""

import logging
import multiprocessing
from multiprocessing.connection import Client, Listener, wait
import os
from pathlib import Path
import sys
import threading
import time


IDLE_SECONDS = 30 * 60
RESOURCE_MODES = ("keep-warm", "release-idle")
log = logging.getLogger(__name__)


def serve_playback(connection, engine):
    from stream import SegmentRenderer

    try:
        renderer = SegmentRenderer(*connection.recv(), engine)
        while (slot := connection.recv()) is not None:
            try:
                # The only client message during a render is cancellation.
                data = renderer.render(slot, connection.poll)
            except Exception as exc:
                log.exception("3D conversion failed")
                connection.send_bytes(b"\0" + str(exc).encode("utf-8", errors="replace"))
                return
            connection.send_bytes(data)
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


def warm_up(engine):
    import numpy as np
    from offline.two_dvr_gpu import GpuStereoRenderer
    from offline.two_dvr_render import DEFAULT_FLAT_FOV_DEG, effective_eye_distance_mm
    from video import HOLE_FILL, STEREO_STRENGTH

    frame = np.full((800, 1920, 3), 64, dtype=np.uint8)
    depth = engine.sequence(1920, 800)(frame)
    if not np.isfinite(depth).all():
        raise RuntimeError("oVDA startup inference returned non-finite depth")
    renderer = GpuStereoRenderer(1920, 800, "flat3d", effective_eye_distance_mm(65, STEREO_STRENGTH),
                                 HOLE_FILL, DEFAULT_FLAT_FOV_DEG)
    renderer.render(frame, depth)


def run_worker(ready, authkey, upstream, checkpoint, logfile, prewarm):
    if sys.stdout is None:
        sys.stdout = open(logfile, "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    logging.basicConfig(level=os.getenv("LIVE3D_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s  %(message)s")
    parent = multiprocessing.parent_process()

    def exit_with_parent():
        parent.join()
        os._exit(0)

    threading.Thread(target=exit_with_parent, daemon=True).start()
    try:
        # Locate the CUDA DLLs without loading PyTorch into the HTTP process.
        dll_directory = None
        if os.name == "nt":
            from importlib.util import find_spec

            torch_lib = Path(find_spec("torch").origin).parent / "lib"
            dll_directory = os.add_dll_directory(str(torch_lib))
            os.environ["PATH"] = os.pathsep.join((str(torch_lib), os.environ["PATH"]))

        from ovda import OvdaEngine

        engine = OvdaEngine(upstream, checkpoint)
        if prewarm:
            warm_up(engine)
        with Listener(("127.0.0.1", 0), backlog=32, authkey=authkey) as listener:
            ready.send(listener.address)
            ready.close()
            while True:
                try:
                    connection = listener.accept()
                except (multiprocessing.AuthenticationError, EOFError, ConnectionError):
                    continue
                threading.Thread(target=serve_playback, args=(connection, engine), daemon=True).start()
    except Exception:
        log.exception("GPU worker failed")
        raise
    finally:
        if dll_directory:
            dll_directory.close()


class GpuWorker:
    def __init__(self, upstream, checkpoint, logfile, *, prewarm):
        self.args = upstream, checkpoint, logfile, prewarm
        self.authkey = os.urandom(32)
        self.process = None
        self.address = None
        self.start_lock = threading.Lock()

    def start(self):
        with self.start_lock:
            if self.process is None or not self.process.is_alive():
                self.close()
                context = multiprocessing.get_context("spawn")
                ready, sender = context.Pipe(duplex=False)
                with ready, sender:
                    process = context.Process(target=run_worker, args=(sender, self.authkey, *self.args),
                                              name="Realtime 3D DLNA GPU", daemon=True)
                    process.start()
                    self.process = process
                    sender.close()
                    log.info("Started GPU worker (PID %d)", process.pid)
                    try:
                        if ready in wait([ready, process.sentinel]):
                            self.address = ready.recv()
                    except EOFError:
                        pass
                    if self.address is None:
                        self.close()
                        raise RuntimeError("GPU worker exited during startup")

    def connect(self):
        self.start()
        return Client(self.address, authkey=self.authkey)

    def status(self):
        if not self.start_lock.acquire(blocking=False):
            return "loading"
        try:
            return "loaded" if self.process is not None and self.process.is_alive() else "idle"
        finally:
            self.start_lock.release()

    def close(self):
        if self.process is not None:
            process = self.process
            self.process = None
            self.address = None
            if process.is_alive():
                process.terminate()
            process.join()
            log.info("Released GPU worker (PID %d)", process.pid)
            process.close()


class PlaybackResources:
    def __init__(self, worker, mode, *, clock=time.monotonic, timer_factory=threading.Timer):
        self.worker = worker
        self.mode = mode
        self.idle_timeout = IDLE_SECONDS if mode == "release-idle" else None
        self.clock = clock
        self.timer_factory = timer_factory
        self.lock = threading.Lock()
        self.active = 0
        self.deadline = None
        self.timer = None
        self.closed = False

    def begin(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("Playback resources are shutting down")
            self.active += 1
            self.cancel_timer()

    def end(self):
        with self.lock:
            self.active -= 1
            if self.active == 0:
                if self.closed:
                    self.worker.close()
                elif self.idle_timeout is not None and self.worker.process is not None:
                    self.deadline = self.clock() + self.idle_timeout
                    self.schedule_expiry(self.idle_timeout)

    def cancel_timer(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.deadline = None

    def schedule_expiry(self, delay):
        expected = self.deadline
        self.timer = self.timer_factory(delay, self.expire, args=(expected,))
        self.timer.daemon = True
        self.timer.start()

    def expire(self, expected):
        with self.lock:
            if self.active or self.deadline != expected:
                return
            remaining = expected - self.clock()
            if remaining > 0:
                self.schedule_expiry(remaining)
                return
            self.cancel_timer()
            self.worker.close()

    def status(self):
        with self.lock:
            return {
                "resource_mode": self.mode,
                "worker": self.worker.status(),
                "active_requests": self.active,
                "idle_timeout_seconds": self.idle_timeout,
            }

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel_timer()
            if not self.active:
                self.worker.close()
