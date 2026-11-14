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
