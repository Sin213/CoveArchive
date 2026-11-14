# CoveArchive

CoveArchive is a project for local preservation of GitHub project releases.
Its intended purpose is to help people keep release assets in a destination
they control, with an index that can support later verification and rescanning.

## Status

**pre-alpha, slice CA-S3** — Windows-first native PySide6 release scanner GUI.

The scope of this slice is project bootstrap, a GitHub URL classifier, and a
release-asset format matcher. These are the foundations for the planned workflow.
The URL parser classifies supported targets, and the format matcher identifies
supported asset suffixes. An offline test suite covers both components.

The GUI uses CA-S2 release discovery through the official GitHub REST API. Expand
releases to inspect all assets; format and prerelease filters run locally. Release
notes are not shown. There are no downloads, SQLite, or archive writes yet.

Python 3.12+, requests (>=2.32,<3), and PySide6 (>=6.11,<7) are required. From the
source tree, launch `python run_covearchive.py`. The developer diagnostic
`python run_covearchive.py --smoke-test` opens the window, processes events, and exits.

Paste a repository or `/releases` URL, then Scan (or press Enter). Tag URLs are not
supported in the UI. Cancel Scan kills and reaps the separate worker process.
A 180 s hard deadline also kills it: very large repositories report **Timed out**,
never a partial listing. The message reads "Timed out after 180 seconds; try again".
Errors retain the URL so you can rescan without restart.
Unauthenticated GitHub API rate limits apply; retry guidance uses backend fields.
The GUI never makes synchronous network requests. A worker watchdog also exits on
parent-pipe EOF or its own deadline, guarding against Windows Python redirectors
that might otherwise leave a grandchild behind. This is a Windows-first source
launcher; frozen packaging and other operating systems are not validated here.

## GitHub URL classification

The classifier contract covers repository, release-list, and release-tag URLs
on github.com and www.github.com over HTTPS.
Schemeless inputs beginning with either supported host are also in scope.
Owner and repository spelling is preserved, with a case-insensitive repository key.
Release tags may contain slash-separated components and percent-encoded UTF-8.
Queries and fragments do not identify a different repository or release.

The intended validation rejects unsupported schemes, credentials, ports,
lookalike hosts, ambiguous paths, and unsafe characters.
Unsupported GitHub page types are not interpreted as release requests.

## Supported release-asset formats

The format matcher contract covers these extensions, matched case-insensitively
for ASCII letters:

- `.appimage` — AppImage
- `.tar.gz` — TAR.GZ
- `.tar` — TAR
- `.dmg` — DMG
- `.zip` — ZIP
- `.7z` — 7z

Matching uses the final filename suffix and requires a non-empty stem.
An optional enabled-format selection limits which actual extensions match.
Unicode characters resembling ASCII extension letters are not accepted.

## Archive layout and index (CA-S4: planning only)

```text
<ARCHIVE_ROOT>\Owner\Repo\
|-- archive-index.json
`-- release-<safe-tag>--<release-id>\
    |-- release.json
    `-- <saved asset filename>
```

Assets sit beside release.json, with no assets\ subfolder. Release folders embed the GitHub release id, never list position; a tag change keeps the established folder.

Original filenames stay unchanged when safe and unclaimed; originals are always preserved in metadata.
Invalid characters, reserved device names and trailing dots/spaces are replaced or removed deterministically; over-long names receive a hash suffix.
Case-insensitive and Unicode-normalization collisions get stable asset-id suffixes, never "(1)" or list-order names.
The conservative path budget is 240 UTF-16 code units, with a 120-unit component limit; no long-path or registry settings are changed.

archive-index.json is the per-project identity and saved-name registry; release.json is the per-release description for reconstruction.
Both use schema_version 1 and provider "github", with stable release/asset ids, saved names, sizes and planned/completed status.
Publisher SHA-256 is recorded when GitHub supplies it; local SHA-256 belongs only to a computed result. No tokens or signed download URLs are stored.
Unknown or newer schema versions are rejected, never overwritten.

**Trust rule:** a completed entry in JSON is only a claim; "publisher verified" requires publisher evidence.
The plan asks CA-S5 to verify real file existence, size, local SHA-256 and publisher SHA-256 when available before any skip.

Rescans plan new releases/assets, retain those removed from GitHub or hidden by filters without deleting them, and flag remote metadata changes for reconciliation.
**Limits:** CA-S4 is pure in-memory planning and metadata validation. No archive folders or JSON files are written, no downloads occur, and no files are verified. The GUI does not use it yet.
Reparse-point, junction, symlink, TOCTOU and concurrent-writer protection are not implemented yet (CA-S5).

**Future transfer defaults (design only, not implemented)**

- 2 concurrent downloads.
- 4 connections per download, subject to server range support.
- Global speed limit unlimited by default and configurable.
- Optional random start delay disabled by default; 1-5 s jitter when enabled.
- Honor authoritative server cooldowns.
- Bounded exponential backoff with jitter.
- API request pacing separate from asset transfer scheduling.
- No bot-detection evasion and no circumvention of GitHub restrictions.

## FUTURE workflow — not implemented

Discovery and filtering are available. Destination selection, downloads and archive
management in the following workflow are FUTURE, not implemented:

1. Paste a GitHub repository or releases URL.
2. Discover releases through the official GitHub REST API.
3. Filter release assets by supported format.
4. Choose a local destination.
5. Download selected assets into per-release folders.
6. Verify downloaded files and maintain an index.
7. Resume interrupted work.
8. Rescan for new releases without duplicating existing downloads.

CoveArchive is not a complete Git history backup.
The planned workflow preserves selected release assets; it does not promise
to mirror every part of a GitHub project.

As a design rule, downloads are never automatically executed or extracted.

## Development

Python 3.12 or newer is required. The test extra declares pytest as a development
dependency. Tests run offline and do not contact GitHub.

From the repository root, run:

```console
python -m pytest
```

The source package lives under `src/`; tests live under `tests/`.
The project uses the MIT license; see `LICENSE` for its terms.
