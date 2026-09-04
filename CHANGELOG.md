# Changelog

All notable changes to **openMontagePlus** are documented here. This project is
a web-platform fork of [OpenMontage](https://github.com/calesthio/OpenMontage)
(FFmpeg-based end-to-end AI video pipeline).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-04

First public open-source release of the **openMontagePlus** fork.

### Added
- **Web platform (`om/`)** — a Spring-Boot-style HTTP service on top of the
  core FFmpeg pipeline, exposing the full creative flow over REST + a single-page UI:
  - Conversational creation studio (chat → pipeline selection → asset upload →
    progress board → finished video playback/download).
  - **Account center** with real balance, recharge, and per-job cost ledger
    (no mock membership / billing pages — costs are metered for real).
  - **Real cost metering** (`om/meter.py`) with a provider price table
    (image / TTS / stock-footage generation).
  - **Hosted billing mode**: jobs run on server-owned keys are metered as
    `hosted=true` and debited from the user balance; users can also bring their
    own keys (BYOK) and pay nothing to the platform.
  - **My Works** and **Public Gallery** with auto-extracted cover posters.
  - **Template marketplace** (10 curated templates, search + category tabs,
    one-click "generate now" that drives the real pipeline).
  - **Server Tools status** page (key validation, model routing, provider health).
- User auth with salted password hashing (`pbkdf2_hmac`, 120k iterations) and
  token sessions; welcome bonus credited on registration.
- Docker deployment (`docker-compose.yml`, `deploy/run_build.sh`).
- Repository storefront: rewritten `README` / `README_zh-CN`, localized
  `CONTRIBUTING`, issue & PR templates, CI workflow, FUNDING/CODEOWNERS pointed
  at the fork owner.

### Changed
- Removed the "viral topic radar" and "avatar/spokesperson studio" modules
  (replaced by the curated template marketplace).
- Metering precision raised from 4 → 6 decimal places so micro-cost jobs
  (e.g. a few seconds of TTS) are no longer rounded to zero.

### Security
- No API keys are committed. Real provider keys live only in `.env` /
  `projects/` (git-ignored); `.dockerignore` keeps them out of the image.
- `.workbuddy/`, `.om/` runtime state, `*.db`, and large build artifacts are
  excluded from version control.

[1.0.0]: https://github.com/sunqionggang/openMontagePlus/releases/tag/v1.0.0
