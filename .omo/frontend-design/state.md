# Frontend Design State

## Current Objective

Add a persistent per-streamer auto-download preference to tracked cards without redesigning the operational interface.

## Locked Decisions

- Preserve the existing sharp, brutalist visual language documented in `DESIGN.md`.
- Auto-download is a backend preference; it must work with the browser closed.
- Accessibility and clear state feedback outrank decoration.

## Design Brief

Primary user: a local operator monitoring several streamers. Primary journey: enable automatic recording once and trust the existing poller to start future live sessions. Stress context: keyboard-only operation and narrow mobile layout.

## Verification Matrix

- Backend tests cover persistence, migration, polling, and API behavior.
- Browser QA covers mouse and keyboard toggle operation, save state, rollback path, and 375/768/1280 widths.
- Final implementation review receives screenshots and test evidence.

## Design Debt Register

No new design or accessibility debt accepted for this feature.

## Evidence Index

- Runtime and interaction transcript: `.omo/evidence/auto-download/runtime.txt`
- Responsive/state screenshots: `.omo/evidence/auto-download/*.png`
- Automated behavior: `tests/test_auto_download.py`
