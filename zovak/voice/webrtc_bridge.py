"""Audio bridge between two WebRTC servers that can't connect to each other directly.

Meta's WhatsApp calls and OpenAI's GPT-Live are both ICE-lite servers: each only
answers connection checks and never starts them, so a direct Meta↔OpenAI call
never gets audio (confirmed from their SDPs). A browser works with GPT-Live
because it is a full WebRTC client that starts the checks. This bridge plays
that role twice:

    WhatsApp caller ─ Meta ◄── leg 1 (we answer Meta's offer) ──► bridge
    bridge ◄── leg 2 (we offer, like the website) ──► OpenAI GPT-Live

and copies audio frames both ways. The sideband (booking rules, guardrails)
attaches to the GPT-Live session exactly as for any other call.
"""
from __future__ import annotations

import asyncio
import fractions
import logging
import time

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms
_TIME_BASE = fractions.Fraction(1, SAMPLE_RATE)


class RelayTrack(MediaStreamTrack):
    """An outgoing audio track fed from the other leg, paced at real time.

    Sends silence until audio arrives (Meta expects our side to send first),
    and drops backlog so latency can't build up.
    """

    kind = "audio"

    def __init__(self):
        super().__init__()
        self.queue: asyncio.Queue[av.AudioFrame] = asyncio.Queue(maxsize=10)
        # One fixed format for everything we send: the Opus encoder can't switch
        # between our mono silence and the other leg's (often stereo) audio mid-call.
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        self._start: float | None = None
        self._pts = 0
        self.forwarded = 0

    def push(self, frame: av.AudioFrame) -> None:
        frame.pts = None  # let the resampler restart timing; recv() sets our own pts
        for converted in self._resampler.resample(frame):
            if self.queue.full():
                self.queue.get_nowait()  # drop the oldest frame: stay real-time
            self.queue.put_nowait(converted)

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.monotonic()
        wait = self._start + self._pts / SAMPLE_RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            frame = self.queue.get_nowait()
            self.forwarded += 1
        except asyncio.QueueEmpty:
            frame = silence()
        frame.pts, frame.time_base = self._pts, _TIME_BASE
        self._pts += frame.samples
        return frame


def silence() -> av.AudioFrame:
    frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    frame.sample_rate = SAMPLE_RATE
    return frame


async def _pump(source: MediaStreamTrack, target: RelayTrack, name: str, stats: dict) -> None:
    """Copy decoded audio frames from one leg's incoming track into the other's outgoing one."""
    try:
        while True:
            frame = await source.recv()
            stats[name] += 1
            target.push(frame)
    except Exception:  # the track ends when its leg closes
        logger.info("Audio pump %s stopped after %d frames", name, stats[name])


async def _gathered(pc: RTCPeerConnection) -> None:
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.05)


class AudioBridge:
    """One WhatsApp call: leg 1 to Meta, leg 2 to GPT-Live, audio copied between them."""

    def __init__(self, label: str):
        self.label = label
        self.meta = RTCPeerConnection()
        self.openai = RTCPeerConnection()
        self.to_meta = RelayTrack()  # what the caller hears (Nofa)
        self.to_openai = RelayTrack()  # what Nofa hears (the caller)
        self.stats = {"caller→nofa": 0, "nofa→caller": 0}
        self._tasks: set[asyncio.Task] = set()
        self._watch(self.meta, "meta")
        self._watch(self.openai, "openai")

    def _watch(self, pc: RTCPeerConnection, name: str) -> None:
        @pc.on("connectionstatechange")
        async def _state() -> None:
            logger.info("Bridge %s: %s leg %s", self.label, name, pc.connectionState)

        @pc.on("track")
        def _track(track: MediaStreamTrack) -> None:
            if track.kind != "audio":
                return
            if name == "meta":
                self._spawn(_pump(track, self.to_openai, "caller→nofa", self.stats))
            else:
                self._spawn(_pump(track, self.to_meta, "nofa→caller", self.stats))

    def _spawn(self, work) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def openai_offer(self) -> str:
        """Leg 2: our SDP offer for GPT-Live, made the way the website's browser does."""
        self.openai.addTrack(self.to_openai)
        self.openai.createDataChannel("oai-events")
        await self.openai.setLocalDescription(await self.openai.createOffer())
        await _gathered(self.openai)
        return self.openai.localDescription.sdp

    async def openai_answer(self, sdp: str) -> None:
        await self.openai.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    async def meta_answer(self, offer_sdp: str) -> str:
        """Leg 1: answer Meta's offer. As a full ICE agent we start the checks Meta waits for."""
        await self.meta.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        self.meta.addTrack(self.to_meta)
        await self.meta.setLocalDescription(await self.meta.createAnswer())
        await _gathered(self.meta)
        return self.meta.localDescription.sdp

    def start_stats(self, every: float = 5.0) -> None:
        async def report() -> None:
            while True:
                await asyncio.sleep(every)
                logger.info("Bridge %s audio frames: %s (sent to caller %d, to Nofa %d real)",
                            self.label, self.stats, self.to_meta.forwarded, self.to_openai.forwarded)
        self._spawn(report())

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(self.meta.close(), self.openai.close(), return_exceptions=True)
