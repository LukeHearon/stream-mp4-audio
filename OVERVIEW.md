# Overview

Planning doc for `stream-mp4-audio`. No implementation exists yet — this lays out what needs to be built and the open questions to resolve first.

## Contract to satisfy

Derived from `OSU-Bee-Lab/buzzdetect`, `src/stream/worker.py` (`WorkerStreamer`) and `src/stream/audio.py`, which is the actual caller this library needs to work with (same contract as the sibling project [`stream-wma`](https://github.com/LukeHearon/stream-wma) had to satisfy for `.wma`):

- `a_file.track = sf.SoundFile(path)` — open a track lazily; no upfront full decode, and for `.mp4` specifically, no upfront full demux either (video packets should never be read off disk just to reach audio ones).
- `track.samplerate`, `track.channels` — static properties read from the audio stream's header, not the container as a whole.
- `track.frames` — used (via `get_duration`) as `frames / samplerate` to get file duration in seconds. Buzzdetect only special-cases this for mp3 because those headers can be wrong (dead-battery recorders); it treats the result as a best-effort estimate and self-corrects on short reads. **If MP4 audio-track headers report duration reliably, we don't need that estimate-and-correct dance — just return the header value.** Needs verification against real-world files (see Open questions).
- `track.seek(sample_index)` — absolute, sample-indexed seek. Called once per chunk in `queue_chunk`, not incrementally, so it must be efficient for both:
  - small forward jumps (the common case — sequential ~200s chunks marching through a file), and
  - the general case (a caller could seek anywhere).
- `track.read(n_samples, dtype=np.float32)` — read forward from the current position, returning an `np.ndarray`. Shape convention: `(n_samples,)` for mono, `(n_samples, channels)` for multichannel (buzzdetect does `np.mean(samples, axis=1)` when `channels > 1`).
- `track.tell()` — current sample position. Used in `handle_bad_read` to figure out how far a short read actually got.
- **Short reads must degrade, not raise.** If asked for `read_size` samples but only `n_samples < read_size` are available (truncated/corrupt file, or end of file), return what's available rather than throwing. Buzzdetect checks `len(samples) < read_size` and treats it as "this was the last readable chunk."

The library's own class doesn't need to literally subclass `soundfile.SoundFile` — buzzdetect would just need a one-line dispatch on file extension to construct the right track type (there's no such dispatch today; `_chunk_file` calls `sf.SoundFile(a_file.path_audio)` directly). Match the interface, not the inheritance.

## Core technical problem

MP4 (ISO BMFF) is a container that can carry multiple streams — typically H.264/H.265 video plus an AAC (or occasionally ALAC/other) audio track. There's no pure-Python decoder in common use for AAC; this needs to wrap a real decoder. As with `stream-wma`, the practical option is **PyAV** (FFmpeg bindings): it exposes container-level, per-stream demuxing and per-frame decode without loading a whole file into memory, and it lets us select only the audio stream so video packets are never touched.

Two problems layer on top of the base decode:

1. **Stream selection.** `av.open()` gives you every stream in the container; the audio stream must be identified (usually `container.streams.audio[0]`, but confirm this holds for real source files — some recorders may tag audio unusually, or a file could carry more than one audio stream) and demuxing must be scoped to just that stream index so PyAV doesn't hand back — or worse, decode — video packets.
2. **Sample-accurate seek.** FFmpeg/MP4 seeking is **not sample-accurate** — it seeks to the nearest preceding sync sample, not an arbitrary sample. AAC frames are typically all independently decodable (unlike video B/P-frames), which may make MP4 audio seek granularity finer and more predictable than WMA/ASF was — but this needs verification, not assumption. Regardless of granularity, honoring `seek(sample_index)` + `read(n)` precisely will need to:
   1. On seek, do a stream-level seek to the nearest position at or before the target, then decode-and-discard forward until the exact target sample is reached.
   2. Keep a small internal buffer of decoded-but-unconsumed samples so consecutive `read()` calls don't each pay a container seek — sequential reads should just keep pulling from the decode stream.
   3. Decide a threshold for when to do a real container seek vs. just decoding forward from the current position (e.g., "if the seek target is close ahead of the current position, don't seek — just decode and discard until we get there").

This buffering/resync logic is the crux of the whole project — get it wrong and it either breaks correctness (misaligned samples) or breaks the performance goal (reseeking from file start every chunk, or accidentally paying for video decode).

## Proposed architecture (sketch, not final)

- `Mp4AudioFile` class: opens the container via PyAV, locates the audio stream, reads its metadata (`samplerate`, `channels`, duration → `frames`) at construction. Video stream, if present, is identified only to confirm it's being excluded — never opened for decode.
- Internal decode cursor: tracks current sample position, holds a small ring buffer of decoded samples not yet consumed by `read()`.
- `seek(sample_index)`: compares target to current position; either fast-forwards through the existing decode stream or issues a real stream-scoped seek + resync.
- `read(n, dtype)`: pulls from the internal buffer, triggering more decoding as needed; returns short if the stream ends or a decode error occurs.
- `tell()`: returns current logical sample position (not the underlying decoder's possibly-approximate position).

## Work breakdown

1. **Research spike:** confirm PyAV can select and decode just the audio stream out of a real MP4 container without touching video, confirm the actual audio codec(s) in use (AAC almost certainly, but verify), confirm container-reported duration is trustworthy, and confirm achievable seek granularity for MP4 audio specifically. This should happen before committing to the architecture above.
2. **Header/metadata reading:** samplerate, channel count, duration → frame count, all scoped to the audio stream.
3. **Sequential read path:** open, demux only the audio stream, decode forward, return chunks — no seek logic yet. Get raw decode-to-numpy correctness right first, and confirm video packets are never read off disk in this path.
4. **Seek + resync logic:** the core problem described above. Needs a decision on the "seek vs. decode-forward" threshold and a correctness test against ground truth.
5. **Truncated/corrupt file handling:** short reads at EOF or on decode error, mirroring buzzdetect's tolerance for bad tails.
6. **Test suite:** fixture MP4 file(s) with an audio track (small and a large/multi-chunk one; ideally one with a video stream present, to prove it's ignored), tests that:
   - many sequential chunked reads reconstruct the same samples as one full decode,
   - `seek()` to an arbitrary sample followed by `read()` matches a full reference decode at that offset,
   - performance stays flat (no O(n²) reseek-from-start behavior) across many sequential chunk reads on a large file,
   - video packets are never decoded (e.g. via a call-count assertion on the video decoder, or timing comparison against a video-present vs. audio-only fixture).
7. **Packaging:** `pyproject.toml`, dependencies (`av`, `numpy`), versioning, license.
8. **Buzzdetect integration:** once this library is solid, wire it into buzzdetect's file dispatch (likely in `src/pipeline/assignments.py` / wherever `AssignFile.track` gets constructed) so `.mp4` files route to `Mp4AudioFile` instead of `sf.SoundFile`. Out of scope for this repo directly, but the reason it exists.

## Open questions

- What audio codec(s) do the real source `.mp4` files actually use? (AAC is the near-universal default; confirm no ALAC/other outliers.)
- Is container-reported duration for the audio track accurate in practice, or does it need the same "estimate + self-correct on short read" treatment as mp3?
- What seek granularity does FFmpeg actually give us for MP4/AAC specifically, and is it in practice finer than the WMA/ASF case (since AAC frames don't require the same kind of predictive decode video frames do)?
- Can a source file have more than one audio stream (e.g. a secondary language/commentary track)? If so, how should the "right" one be selected — first stream, or something else?
- Do we need multichannel support at all, or can we assume mono/stereo only (matching what buzzdetect's recorders produce)?
