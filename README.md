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
The plan requests real file verification before any skip; CA-S5 checks existence, size, local SHA-256 and publisher SHA-256 when available.

Rescans plan new releases/assets, retain those removed from GitHub or hidden by filters without deleting them, and flag remote metadata changes for reconciliation.
**Limits:** CA-S4 remains pure in-memory planning and metadata validation. CA-S5 provides local persistence separately; downloads and GUI integration remain future work.

## CA-S5: persistence

Authorize an existing local NTFS directory with `ArchiveRoot.open`, then use pinned project/release handles. Assets stream through exclusive temps, flush, publish without replacement and verify through the published name. Identical retries report `already_present`; conflicting destinations are preserved. Only real-byte verification authorizes a completed record.

`project_writer` holds an exclusive OS project lock. Commit release manifests first and the index last, using the loaded index SHA-256 as a compare-and-swap token. Loading reports corruption, unsupported versions and index/release mismatches without repairing them. Corrupt or unsupported manifests must be explicitly quarantined before replacement. Recovery removes only idle, regular files matching `.covearchive-tmp-<32 lowercase hex>.part` exactly. Exact internal names are reserved by role: `archive-index.json` and `.covearchive.lock` in project directories, `release.json` in release directories, and exact temporary-pattern names in both. Other `.covearchive` prefixes and quarantine-like asset names are allowed.

| Guarantee class | Scope |
| --- | --- |
| Handle-anchored | On an authorized local NTFS root: pinned ancestors and relative handles reject traversal, reparse points, junctions, symlinks, directory swaps and case/8.3 aliases below the root; exclusive temp creation; no-replace asset publication; size and hash from one read handle. |
| Best-effort / not guaranteed | Components above the root resolve once before its identity is pinned. Remote and non-NTFS volumes are rejected where detectable. `FlushFileBuffers` covers files only, with no directory-flush or power-loss guarantee. Held manifests deny in-place writes. A non-cooperating process with delete-sharing can rename-replace a manifest during commit; unlink checks before and after publication detect namespace changes on a best-effort basis, without an atomic filesystem compare-and-replace. Other same-user/admin processes or antivirus may alter closed files, so completion is point-in-time verification. Verification rejects hard links (link count must be one), but later link creation cannot be prevented. |

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
