# CLAUDE.md

See `README.md` for project goals and `OVERVIEW.md` for the planned work breakdown and open questions.

No implementation exists yet. Start with the research spike in `OVERVIEW.md` (work breakdown item 1) before writing any decoder code — confirm PyAV's audio-only stream selection and real codec/seek behavior against an actual `.mp4` fixture first.

No test fixture is checked in yet. A real `.mp4` file with an audio track (ideally alongside a video stream, to prove it's ignored) will need to be added under `tests/fixtures/`.
