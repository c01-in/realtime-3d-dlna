"""Check the playback idle deadline without waiting in real time."""

import unittest

from worker import IDLE_SECONDS, PlaybackResources


class ManualTimer:
    def __init__(self, delay, callback, args):
        self.delay = delay
        self.callback = callback
        self.args = args
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback(*self.args)


class FakeWorker:
    process = None

    def close(self):
        self.process = None


class IdleDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.worker = FakeWorker()
        self.resources = PlaybackResources(self.worker, "release-idle",
                                           clock=lambda: self.now, timer_factory=ManualTimer)

    def test_keep_warm_keeps_worker_until_shutdown(self):
        resources = PlaybackResources(self.worker, "keep-warm",
                                      clock=lambda: self.now, timer_factory=ManualTimer)
        self.worker.process = object()
        resources.begin()
        resources.end()
        self.assertIsNone(resources.timer)
        self.assertIsNotNone(self.worker.process)
        resources.close()
        self.assertIsNone(self.worker.process)

    def test_first_request_can_finish_without_starting_worker(self):
        self.resources.begin()
        self.resources.end()
        self.assertIsNone(self.resources.timer)
        self.assertIsNone(self.worker.process)

    def test_concurrent_requests_and_resume_reset_the_deadline(self):
        self.assertEqual(IDLE_SECONDS, 1800)
        self.resources.begin()
        self.worker.process = object()
        self.resources.begin()
        self.resources.end()
        self.assertIsNone(self.resources.timer)
        self.now = 10
        self.resources.end()
        old_timer = self.resources.timer
        self.assertEqual(old_timer.delay, 1800)

        self.now = 1809
        old_timer.fire()
        self.assertIsNotNone(self.worker.process)
        self.assertEqual(self.resources.timer.delay, 1)
        rearmed = self.resources.timer
        self.resources.begin()
        self.assertTrue(rearmed.cancelled)
        self.now = 1810
        rearmed.fire()
        self.assertIsNotNone(self.worker.process)
        self.resources.end()

        self.now = 3609
        self.resources.timer.fire()
        self.assertIsNotNone(self.worker.process)
        self.now = 3610
        self.resources.timer.fire()
        self.assertIsNone(self.worker.process)
        self.assertIsNone(self.resources.timer)

        self.resources.begin()
        self.worker.process = object()
        self.resources.end()
        self.assertEqual(self.resources.timer.delay, 1800)


if __name__ == "__main__":
    unittest.main()
