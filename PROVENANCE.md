# ReelPilot provenance

ReelPilot is a newly authored, clean-room-style implementation based on observable game
behavior and a written functional specification.

Its purpose is educational: the project demonstrates Python application design, Rust and
Windows input safety, OpenCV screen vision, deterministic feedback control, automation,
SQLite, concurrency, packaging, and QA. It is intended for private single-player learning,
not competitive play, leaderboards, anti-cheat bypasses, or gaining an advantage over
other players.

## Excluded material

The public repository and release must not contain:

- Source code, compiled helpers, or templates from
  [Lixian-Zhang/StardrewAutoFishing](https://github.com/Lixian-Zhang/StardrewAutoFishing).
- Source code or ML assets from
  [mrmattkennedy/Stardew-Fisher](https://github.com/mrmattkennedy/Stardew-Fisher).
- Stardew Valley sprites, fonts, XNB data, screenshots, or other game assets.
- Private gameplay recordings or crops used for local black-box testing.

Both referenced repositories influenced the behavioral requirements. Their unlicensed
implementation material is not redistributed by ReelPilot.

## Newly authored components

- Python automation, vision, control, statistics, recording, dashboard, and Windows
  integration code under `src/reelpilot`.
- Rust input helper under `native/reelpilot-input`.
- Synthetic tests and terminal-only documentation screenshots.
- Packaging, CI, and release scripts.

The fish and item catalog contains manually authored factual interoperability metadata:
English canonical names, qualified identifiers, base sell prices, numeric minigame
difficulty, and motion types for the supported vanilla version. It was validated against
the user's installed Stardew Valley 1.6.15.24356 data schemas and the public
[fish-data](https://stardewvalleywiki.com/Modding%3AFish_data),
[object-data](https://stardewvalleywiki.com/Modding%3AObjects), and
[difficulty-tag](https://stardewvalleywiki.com/Modding%3AContext_tags) documentation. It
includes no descriptions, art, game data files, or extracted font data.

## Private validation

Locally captured sessions may be used as black-box inputs to compare detector coordinates,
timing, and controller outputs. They are ignored by Git and are never copied into public
fixtures, documentation, build artifacts, or releases.

This document records engineering provenance; it is not legal advice or a legal opinion.
