# Delivery

Read by `/ship` and its subagents. Antoine's standing instructions for delivering a spec or a lot of tickets.

## Checks

- Verify: `just qa` (installs the pinned tools, ruff format and check, ty, pytest); must finish green on every ticket
  and on the integration branch, and must not reformat files the ticket did not touch.
- No server or page here: compression-suite is a CLI. The manual check is the CLI itself, run with `uv run
  --python=3.13 compression_suite ...` on fabricated samples and on the real-file copies allowed below.
- Browser playback check (video tickets): headless Chrome (`/usr/bin/google-chrome`) or Firefox (`/usr/bin/firefox`),
  driven by Playwright installed in a scratch directory outside the repo; load the file in a `<video>` element and
  read `error`, `readyState`, `videoWidth`/`videoHeight` and the console, not only whether the page loads.
- A fresh worktree has no `.tools/`: `just qa` runs `just install-tools`, which downloads the pinned tools.
- Real video encodes (x265, `just qa` on video tickets) run one at a time: on this laptop, several at once crashed
  the session twice during lot 3.

## Off-limits

- Real data: never written, moved or deleted. Read-only use allowed only for manual checks: copy one or two videos
  from `~/Syncthing-shares/S23` and one iPhone HEIC from `~/Syncthing-shares/Xiaomi/Messages` into `/tmp`, work on the
  copies, delete the copies at the end. Never touch the originals.
- The repo is public: no real file, extract, frame or metadata dump of a real file is ever committed. Test fixtures
  are fabricated by a versioned script. Nothing personal in issue comments: no GPS value, no real path, no image.
- Paid services: no real Tinify call. Tests mock `tinify`. Never put a Tinify key anywhere; never run
  `compress-image` on the real configuration.
- Nothing outside this repo is modified: the caller's checkout is read-only for this work, and its dev server (port 27183) is never started or stopped.

## Branches and commits

- Integration branch: `<spec-slug>-lot-<n>` (e.g. `rangement-medias-lot-3`), created from `master`. Ticket branches:
  `<integration-branch>-<ticket>`.
- Running `/ship` is Antoine's explicit request to commit on the integration and ticket branches, never on `master`.
  Add only the files the ticket touched.
- Commit messages: English, in the style of `git log` (imperative, what changed for the user). Never `Closes #N`.
- At the end: push nothing, close no issue. Antoine reviews and merges the integration branch himself; after his
  push, each ticket gets a closing comment (`gh issue comment`) with its acceptance boxes ticked.

## Docs

- No spec is kept in this repo: the issues (parent and tickets) carry the contract decisions, and `README.md`
  documents each command.

## Real usage

What the product really meets; reviewers judge `blocking` against it.

- Caller: one desktop app, through the CLI in `--json` mode. No other caller.
- Inputs: files from phones. Galaxy S23 videos (HEVC in MP4), iPhone HEIC photos, JPEG.
- Some inputs are cut short, when a Syncthing sync was interrupted.
- The caller runs several calls in parallel to prepare the next files (inspect, preview, display copy). It runs only one
  export at a time.

## Project edge cases

Failure modes this project has already met; reviewers check them on top of the generic list:

- stdout must hold only the JSON envelope in `--json`: any stray print or tool output breaks the caller;
- a new error code only when the caller reacts differently; otherwise the reason goes in the message;
- a decoder that accepts a damaged file silently (truncated HEIC with missing tiles);
- metadata lost or rewritten by the encoder (orientation applied twice, colour profile, dates), shown as no loss;
- a write killed halfway leaving a partial output or a temporary file next to it; the original's bytes or mtime
  changed;
- a tool taken from the `PATH` instead of the pinned one; a pinned-tool upgrade that changes decoding (re-run
  `inspect` on real files, none must be flagged corrupt).
