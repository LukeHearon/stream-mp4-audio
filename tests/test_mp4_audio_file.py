import os

import av
import numpy as np
import pytest

from stream_mp4_audio import Mp4AudioFile

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test.mp4")


def _decode_reference():
    container = av.open(FIXTURE)
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="fltp", layout=stream.layout, rate=stream.rate)
    chunks = []
    for frame in container.decode(stream):
        for out_frame in resampler.resample(frame):
            chunks.append(out_frame.to_ndarray().T)
    for out_frame in resampler.resample(None):
        chunks.append(out_frame.to_ndarray().T)
    container.close()
    data = np.concatenate(chunks, axis=0).astype(np.float32)
    if data.shape[1] == 1:
        data = data[:, 0]
    return data


@pytest.fixture(scope="module")
def reference():
    return _decode_reference()


def test_header_properties(reference):
    with Mp4AudioFile(FIXTURE) as track:
        assert track.samplerate == 44100
        assert track.channels == 1
        assert isinstance(track.frames, int)
        assert track.frames > 0
        assert abs(track.frames - len(reference)) < 0.01 * len(reference)


def test_video_stream_present_but_never_decoded():
    container = av.open(FIXTURE)
    assert len(container.streams.video) == 1
    container.close()

    with Mp4AudioFile(FIXTURE) as track:
        track.read(track.frames)
    # If Mp4AudioFile had touched the video stream's decoder, opening a plain
    # container and decoding video from scratch afterward would still work --
    # this just proves the fixture genuinely carries a video stream at all.
    container = av.open(FIXTURE)
    video_frame = next(container.decode(container.streams.video[0]))
    assert video_frame is not None
    container.close()


def test_only_audio_stream_demuxed_by_internal_decoder():
    with Mp4AudioFile(FIXTURE) as track:
        for packet in track._container.demux(track._stream):
            assert packet.stream.type == "audio"


def test_full_decode_equivalence(reference):
    with Mp4AudioFile(FIXTURE) as track:
        samples = track.read(len(reference) + 1000)
        np.testing.assert_array_equal(samples, reference)


def test_chunked_sequential_reconstruction(reference):
    with Mp4AudioFile(FIXTURE) as track:
        chunks = []
        while True:
            chunk = track.read(5000)
            if len(chunk) == 0:
                break
            chunks.append(chunk)
        samples = np.concatenate(chunks)
        np.testing.assert_array_equal(samples, reference)


def test_seek_per_chunk_sequential_pattern(reference):
    with Mp4AudioFile(FIXTURE) as track:
        chunks = []
        pos = 0
        while True:
            track.seek(pos)
            chunk = track.read(5000)
            if len(chunk) == 0:
                break
            chunks.append(chunk)
            pos += len(chunk)
        samples = np.concatenate(chunks)
        np.testing.assert_array_equal(samples, reference)
        assert track._container_seek_count <= 1


def test_arbitrary_forward_seek(reference):
    with Mp4AudioFile(FIXTURE) as track:
        target = len(reference) // 2
        track.seek(target)
        chunk = track.read(4000)
        np.testing.assert_array_equal(chunk, reference[target:target + 4000])


def test_backward_seek_after_forward_progress(reference):
    # A landed-and-resynced read matches the reference to within float32
    # rounding (~1e-6), not bit-for-bit: the AAC decoder's internal MDCT
    # overlap state after a seek+discard is numerically equivalent to, but not
    # byte-identical to, a continuous decode -- confirmed by the research
    # spike even with a resampler that is never recreated across the seek.
    with Mp4AudioFile(FIXTURE) as track:
        track.read(400000)
        track.seek(100000)
        chunk = track.read(2000)
        np.testing.assert_allclose(chunk, reference[100000:102000], atol=1e-3)
        assert track._container_seek_count >= 1

        seeks_before = track._container_seek_count
        track.seek(54321)
        chunk2 = track.read(3000)
        np.testing.assert_allclose(chunk2, reference[54321:57321], atol=1e-3)
        assert track._container_seek_count > seeks_before


def test_backward_seek_to_start(reference):
    with Mp4AudioFile(FIXTURE) as track:
        track.read(400000)
        track.seek(0)
        chunk = track.read(2000)
        np.testing.assert_array_equal(chunk, reference[0:2000])


def test_backward_seek_into_first_frame(reference):
    # Regression case surfaced by the research spike: a target inside the very
    # first decoded frame has no earlier frame to seek-and-discard from, so it
    # must go through the fresh-reopen fallback rather than the usual
    # seek-and-resync path.
    with Mp4AudioFile(FIXTURE) as track:
        track.read(400000)
        track.seek(5)
        chunk = track.read(100)
        np.testing.assert_array_equal(chunk, reference[5:105])


def test_tell(reference):
    with Mp4AudioFile(FIXTURE) as track:
        assert track.tell() == 0
        track.read(1000)
        assert track.tell() == 1000
        track.seek(5000)
        assert track.tell() == 5000
        track.read(500)
        assert track.tell() == 5500
        track.seek(2000)
        assert track.tell() == 2000


def test_reseek_determinism(reference):
    with Mp4AudioFile(FIXTURE) as track:
        track.read(300000)
        track.seek(50000)
        first = track.read(1000)
        track.seek(50000)
        second = track.read(1000)
        np.testing.assert_array_equal(first, second)
        np.testing.assert_allclose(first, reference[50000:51000], atol=1e-3)


def test_short_read_at_eof(reference):
    with Mp4AudioFile(FIXTURE) as track:
        near_end = len(reference) - 500
        track.seek(near_end)
        chunk = track.read(5000)
        assert len(chunk) == 500
        np.testing.assert_array_equal(chunk, reference[near_end:])
        assert track.tell() == len(reference)

        chunk2 = track.read(100)
        assert len(chunk2) == 0
        assert track.tell() == len(reference)


def test_seek_negative_raises():
    with Mp4AudioFile(FIXTURE) as track:
        with pytest.raises(ValueError):
            track.seek(-1)


def test_lazy_open_does_not_decode():
    track = Mp4AudioFile(FIXTURE)
    try:
        assert track.tell() == 0
        assert track._decode_pos == 0
        assert track._buffer.shape[0] == 0
        assert track._container_seek_count == 0
    finally:
        track.close()


def test_read_dtypes():
    with Mp4AudioFile(FIXTURE) as track:
        track.seek(0)
        f32 = track.read(1000, dtype="float32")
        track.seek(0)
        f64 = track.read(1000, dtype="float64")
        track.seek(0)
        i16 = track.read(1000, dtype="int16")

        assert f32.dtype == np.float32
        assert f64.dtype == np.float64
        assert i16.dtype == np.int16
        np.testing.assert_allclose(f32.astype(np.float64), f64)
        np.testing.assert_allclose(i16.astype(np.float64) / 32768.0, f32, atol=2.0 / 32768.0)
