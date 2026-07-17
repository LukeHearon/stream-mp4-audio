# stream-mp4-audio

A Python library for lazily streaming decoded audio samples out of the audio track of `.mp4` files, without demuxing/decoding video or preconverting to an intermediate `.wav`.

## Goal

[`soundfile`](https://github.com/bastibe/python-soundfile) gives you a `SoundFile` object you can `seek()` and `read()` like a tape — but it has no MP4 support (libsndfile doesn't touch ISO BMFF containers). The usual workaround is shelling out to `ffmpeg` to extract/convert the audio track to `.wav` first, then reading that with `soundfile`. This project skips the intermediate file: a `SoundFile`-alike for `.mp4` that supports the same `seek`/`read` contract, decoding directly from the container's audio stream via [PyAV](https://github.com/PyAV-Org/PyAV), video stream untouched.

## Installation

```bash
pip install -e .
```

Requires `av` and `numpy` (declared in `pyproject.toml`); no separate `ffmpeg` binary or intermediate `.wav` file is needed — PyAV bundles its own decoder libraries.

## Usage

```python
from stream_mp4_audio import Mp4AudioFile

track = Mp4AudioFile("recording.mp4")
track.samplerate   # e.g. 44100
track.channels     # e.g. 1
track.frames       # total sample count, from header duration

track.seek(sample_index)
samples = track.read(n_samples, dtype="float32")  # -> np.ndarray of decoded floats
track.tell()        # current sample position

track.close()  # or use `with Mp4AudioFile(...) as track:`
```

`read()` returns `(n_samples,)` for mono or `(n_samples, channels)` for multichannel, and returns fewer samples than requested (never raises) at end of file. `dtype` accepts `"float32"` (default), `"float64"`, or `"int16"`.

## Constraints

- **Audio-only demux.** The container may carry a video stream alongside audio; only the audio stream is opened/demuxed/decoded. Video packets must never be touched, even to skip past them.
- **No intermediate file.** No shelling out to `ffmpeg`, no writing a `.wav` (or any other file) to disk as a preprocessing step. Decode happens in-process, directly from the source `.mp4`.
- **Lazy loading.** Never decode more of the file than has been requested. Files may be multiple gigabytes; decoding a whole file into memory is not an option.
- **Cheap sequential reads.** The typical access pattern is many short, contiguous reads in sequence over a long file (e.g. ~200s chunks marching through a multi-hour recording). Re-seeking from the start of the file for every read is too slow — the decoder position must be tracked and reused across calls.
- **Reliable duration.** If MP4 headers reliably encode total duration/frame count for the audio track, use that directly. (Contrast with the MP3 case, where header-reported duration/frame count is sometimes wrong for files from recorders that died mid-recording, forcing duration to be estimated as `frames / samplerate` with a fallback to detecting truncated reads at analysis time — see `OVERVIEW.md`.)

## Motivating use case

This library exists to plug into [buzzdetect](https://github.com/OSU-Bee-Lab/buzzdetect), an acoustic monitoring pipeline that currently streams audio via `soundfile.SoundFile` in `src/stream/worker.py` (`WorkerStreamer`). Buzzdetect chunks long recordings into fixed-length windows and pulls each chunk with a `seek()` + `read()` pair, sample-indexed, running potentially many chunks per file. Some of buzzdetect's source recordings arrive as `.mp4` video with an incidental audio track (e.g. trail-camera footage); today the only way to analyze those is a manual `ffmpeg` pass to pull and convert the audio first. `stream-mp4-audio` needs to be a drop-in for buzzdetect's `seek`/`read` access pattern so those files can be analyzed directly, the same way `.wav`/`.mp3` are analyzed today.

See `OVERVIEW.md` for the planned scope of work.

## Status

Implemented. `Mp4AudioFile` (`src/stream_mp4_audio/mp4_audio_file.py`) satisfies the contract above, backed by PyAV. See `REPORT.md` for the research-spike findings the implementation is built on and how they were verified.
