# CLAUDE.md

See `README.md` for project goals, `OVERVIEW.md` for the original work breakdown and open questions, and `REPORT.md` for what the research spike found and how the implementation resolves the open questions.

Test fixture: `tests/fixtures/test.mp4` — a real MP4/AAC audio track (mono, 44.1kHz, 30s) plus a real H.264 video track, both genuinely encoded (not fabricated bytes), used for decode/seek/read testing. It is synthetic (generated via PyAV's own encoders, not a field recording) — see `REPORT.md`'s limitations section before treating it as representative of real trail-camera output.
