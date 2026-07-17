# REPORT: stream-mp4-audio implementation

## What was built

`Mp4AudioFile` (`src/stream_mp4_audio/mp4_audio_file.py`), a `soundfile.SoundFile`-alike
for the audio track of `.mp4` files, backed by PyAV. Matches the target API from
`README.md`:

```python
track = Mp4AudioFile("recording.mp4")
track.seek(sample_index)
samples = track.read(n_samples, dtype="float32")
```

Also: `.samplerate`, `.channels`, `.frames`, `.tell()`, context-manager support, and
graceful short reads at EOF (never raises). Video, if present, is never demuxed past
the container-header level. Tests: `tests/test_mp4_audio_file.py`, 16 tests, all
passing against the fixture (`tests/fixtures/test.mp4`, 30s mono AAC/44.1kHz audio
plus a 4s H.264 video track).

## Decoder choice

Same reasoning as the sibling `stream-wma` project: PyAV, not a subprocessed `ffmpeg`
binary. There is no `ffmpeg` CLI in this environment either — only `av`, which bundles
its own `libavformat`/`libavcodec`. The core requirement (sample-perfect seek + cheap
sequential reads) needs a live, reusable decoder object you can ask "how many samples
have you emitted since a known point" — a subprocess pipe can't answer that without
restarting.

## Research spike (done before writing any decoder code, per `CLAUDE.md`)

No real-world `.mp4` field recording was available in this environment (no network
fetch of arbitrary binary content was appropriate here — see "Known limitations"). The
fixture (`tests/fixtures/test.mp4`) was instead synthesized with PyAV's own encoders: a
440Hz mono tone encoded as genuine AAC, muxed alongside a genuine H.264 video track,
into a real ISO BMFF container — decodable by any standard player, not fabricated
bytes. Every finding below was measured against that fixture, empirically, the same
way `stream-wma`'s spike measured against its `.wma` fixture — nothing here is assumed
from documentation.

1. **Stream selection and demux scoping.** `container.streams.audio[0]` finds the
   audio stream; `container.streams.video` confirms a video stream is present.
   `container.decode(audio_stream)` / `container.demux(audio_stream)`, scoped to just
   that stream object, never yields a single video packet — confirmed by demuxing the
   whole container unscoped (1315 packets, audio+video interleaved) versus scoped to
   audio only (1294 packets, all audio). `Mp4AudioFile` never opens
   `container.streams.video` for anything beyond the header-level stream list, so video
   packets are never read off disk by this library, let alone decoded.

2. **Codec confirmed:** `aac` (AAC), `h264` for video. Matches `OVERVIEW.md`'s
   assumption.

3. **MP4 audio `pts` is exact sample position — unlike WMA/ASF.** MP4 audio streams use
   a sample-rate time_base (`1/44100` on the fixture), so `frame.pts` isn't a
   millisecond-granularity estimate the way ASF's is — it's a sample count. Verified by
   linearly decoding the entire fixture and comparing `frame.pts` against a running sum
   of `frame.samples` at every one of ~1292 frames: **zero mismatches**. This is a
   meaningfully different starting point from `stream-wma`, whose whole landmark-cache
   design exists to work around `pts` *not* being trustworthy. For MP4/AAC no cache is
   needed — any `pts` value can be used directly as an exact sample position.

4. **Container-reported duration is reliable.** `stream.duration * stream.time_base *
   samplerate` gave 1,323,000 samples against 1,323,008 actually decodable (a ~0.0006%
   gap, from this fixture's own synthetic zero-padding on its last encoder frame — not
   a codec quirk). Per `README.md`'s stated preference, `.frames` uses the header value
   directly; no WMA/MP3-style "estimate and self-correct" dance is needed, and `read()`'s
   short-read-at-EOF handling (required regardless) absorbs any gap either direction.

5. **Seek granularity is frame-exact (finer than WMA), but still needs a one-frame
   discard-and-resync — including at position 0.** `OVERVIEW.md` speculated AAC's
   independently-decodable frames might make seeking cleaner than WMA's block codec.
   Half right: a container seek always lands exactly on a frame boundary (a multiple of
   the AAC frame size, 1024 samples here) — much finer and more predictable than ASF's
   millisecond landmarks. But **the first frame decoded after *any* container-level
   seek is measurably corrupt** (up to ~0.5 absolute error against a signal of
   amplitude 0.5, i.e. wrong by roughly half its dynamic range), confirmed across 200
   random backward-seek targets. This matches `stream-wma`'s finding for WMA and has
   the same root cause in spirit: AAC is a transform codec (MDCT with 50% overlap-add
   between frames), and a seek can't supply the previous frame's tail the decoder needs
   to reconstruct the first post-seek frame correctly. The frame after that one is
   correct to within float32 rounding (~1e-6, see point 7).

   Critically, **this also applies to seeking back to `pts=0`** — re-seeking to the
   start with `container.seek(0, ...)` produces just as corrupt a first frame as any
   other target (confirmed directly: same ~0.5 error magnitude). `stream-wma`'s report
   claims "`container.seek(0)` reproduces the exact same first frames as a fresh
   `av.open()`"; that did not hold up under direct test here for MP4/AAC, and is why
   `Mp4AudioFile` treats position 0 as reachable only through a **never-seeked**
   decoder (see point 6), not through `container.seek(0, ...)`.

6. **A naive port of `stream-wma`'s "discard 1, trust the next frame" logic
   overshoots the target — this was caught by testing, not assumed.** A container seek
   lands at the frame boundary *at or before* the target, which means the target
   sample is *inside* the very frame that turns out to be corrupt and must be
   discarded. Discarding it and trusting the next frame always lands strictly *past*
   the target (proven by construction: if `boundary <= target < boundary + frame_size`,
   discarding `[boundary, boundary+frame_size)` leaves the next frame starting at
   `boundary + frame_size > target`, always). The fix: seek to one frame *before* the
   target's frame, so the corrupt/discarded frame is a one-frame throwaway and the
   next (trustworthy) frame is the one actually covering the target. Implemented as
   `_land_on_or_before`: seek, discard, check whether the next frame's `pts <= target`;
   if not, back off by exactly the discarded frame's `.samples` (which is exactly one
   frame_size) and retry. Verified over 150+ random targets: converges in at most 2
   attempts. Targets inside the very first frame (sample `< frame_size`) have no
   earlier frame to back off to — see point 5 — and fall back to a full container
   reopen (confirmed bit-exact against a from-scratch reference decode, repeated 3x),
   decoding forward from true position 0.

   No landmark cache is needed for this backoff, unlike `stream-wma` — because MP4
   `pts` is exact (point 3), "one frame before the target's frame" can be computed
   directly from the target and the just-discarded frame's own `.samples`, without
   needing to have visited that territory before.

7. **Post-resync output is numerically exact, not bit-exact.** Reads immediately
   after a real container seek match a from-scratch linear reference decode to
   float32 rounding precision (~1e-6 absolute, occasionally up to ~3e-4 on this
   fixture) but not always to the bit. Traced this to the decoder's own internal
   state, not resampler reinstantiation — confirmed by re-running the same seek with a
   resampler that is *never* recreated across the seek boundary and seeing the same
   tiny delta. Tests that read across a backward seek compare with
   `np.testing.assert_allclose(..., atol=1e-3)` rather than exact equality for this
   reason; tests confined to pure forward decoding (no seeks) compare bit-exact and
   pass at that bar, confirming the forward-only path has zero drift.

8. **Multiple audio streams:** not testable — no multi-track fixture was built
   (out of scope; would need a second synthetic audio stream and no clear "which one
   is right" signal to test against). `Mp4AudioFile` uses `streams.audio[0]`, same
   choice as `stream-wma` made for WMA and what `OVERVIEW.md` suggested as the default.
   Open question, same as `stream-wma`'s equivalent.

## Architecture

- `Mp4AudioFile.__init__`: opens the container, selects `streams.audio[0]` (raises
  `ValueError` if none), reads `samplerate`/`channels` from the stream header,
  `frames` from header duration. Never opens the video stream for anything beyond the
  header stream list.
- Internal decode cursor (`_decode_pos`, `_buffer`) mirrors `stream-wma`'s design:
  decode-ahead into a small buffer of decoded-but-unconsumed samples, `_position` is
  the external read cursor (`tell()`).
- **Forward seeks never touch the container** — decode-and-discard from the live
  decoder, same fast path as `stream-wma`, for the dominant sequential-chunk access
  pattern.
- **Backward seeks** do a real container seek, landing one frame before the target's
  frame (point 6 above), discard the corrupt frame, then decode-forward the small
  remainder to land exactly on target.
- **Seeks into the first frame** (or any case the backoff can't resolve within a
  bounded number of attempts) fall back to a full container reopen and a forward count
  from true position 0 — always correct, just slower; documented in `stream-wma`'s
  report as an acceptable inherent cost for cold/unvisited territory, same logic
  applies here.
- No landmark cache — MP4's exact `pts` makes it unnecessary (point 3 above).

## Success criteria — verification

**(i) Never loads the whole file into memory / audio-only demux.** `test_lazy_open_does_not_decode`
confirms opening does no decode work up front. `test_only_audio_stream_demuxed_by_internal_decoder`
and `test_video_stream_present_but_never_decoded` confirm the fixture genuinely carries
a video track and that the exact demux pattern `Mp4AudioFile` uses internally
(`container.decode`/`demux` scoped to the audio stream object) never yields a video
packet.

**(ii) Sample-perfect seek/read** (to float32 rounding across a seek, bit-exact within
pure forward decode — see point 7). Covered: full-file decode, chunked sequential
reconstruction, seek-per-chunk sequential pattern, arbitrary forward seek, backward
seek to previously-visited and unvisited territory, backward seek to the very start,
backward seek into the first frame (the case point 6 exists for), re-seek determinism,
short reads at true EOF.

**(iii) Sequential reads are nearly free.** `test_seek_per_chunk_sequential_pattern`
asserts `_container_seek_count <= 1` across a full seek-then-read-per-chunk loop over
the whole fixture. A manual timing check: 265 sequential 5000-sample chunks across the
fixture completed with **0** container-level seeks in ~21ms.

## Known limitations

- **The fixture is synthetic, not a field recording.** No real-world source `.mp4`
  (e.g. actual trail-camera footage) was available in this environment, and fetching
  an arbitrary binary file from the network wasn't an appropriate way to obtain one.
  `tests/fixtures/test.mp4` is a genuinely valid MP4 container with real AAC and H.264
  streams (produced by PyAV's own encoders, not hand-crafted bytes) — decode/seek
  behavior measured against it should generalize, since it exercises the same
  codec/container code paths a real file would. But it hasn't been checked against an
  actual recorder's output, which may have quirks (encoder priming/padding trimmed via
  an edit list, non-constant frame sizes, multiple audio streams) this fixture doesn't
  exhibit. Swap in a real recording under `tests/fixtures/` if one becomes available.
- **Multichannel support** is implemented the same way as `stream-wma` (resample to
  planar float, transpose) and was spot-checked against an ad hoc stereo fixture
  (not committed) with both a pure forward decode and a backward-seek read — both
  matched a from-scratch reference. Not covered by the committed test suite, which
  is mono-only, matching `stream-wma`'s equivalent gap.
- **A cold jump into unvisited backward territory costs decode time proportional to
  distance**, same inherent tradeoff `stream-wma` documented for WMA — not a bug,
  just the nature of doing a real container seek instead of an O(1) lookup.
- **`dtype` support** covers `float32` (default), `float64`, and `int16`, matching
  `stream-wma` and the only dtype buzzdetect actually requests (`float32`).

## Not done (explicitly out of scope)

Buzzdetect integration (`OVERVIEW.md` work-breakdown step 8) — lives in a separate
repository not in this session's scope, and `OVERVIEW.md` itself marks it "out of
scope for this repo directly."
