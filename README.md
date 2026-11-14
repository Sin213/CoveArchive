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

archive-index.json is the per-project identity and saved-name registry; release.json is per-release metadata. CA-S5.1 returns validated unindexed manifests for detect-only recovery; it does not repair or rebuild the index.
Both use schema_version 1 and provider "github", with stable release/asset ids, saved names, sizes and planned/completed status.
Publisher SHA-256 is recorded when GitHub supplies it; local SHA-256 belongs only to a computed result. No tokens or signed download URLs are stored.
Unknown or newer schema versions are rejected, never overwritten.

**Trust rule:** a completed entry in JSON is only a claim; "publisher verified" requires publisher evidence.
The plan requests real file verification before any skip; CA-S5 checks existence, size, local SHA-256 and publisher SHA-256 when available.

Rescans plan new releases/assets, retain those removed from GitHub or hidden by filters without deleting them, and flag remote metadata changes for reconciliation.
**Limits:** CA-S4 remains pure in-memory planning and metadata validation. CA-S5 provides local persistence separately; downloads and GUI integration remain future work.

## CA-S5: persistence

Authorize an existing local NTFS directory with `ArchiveRoot.open`, then use pinned project/release handles. Assets stream through exclusive temps, flush, publish without replacement and verify through the published name. Identical retries report `already_present`; conflicting destinations are preserved. Only real-byte verification authorizes a completed record. Verification rejects size mismatches before hashing. Single UTF-16-unit asset names such as `a`, `1`, and `é` publish without replacing existing asset bytes.

`project_writer` holds an exclusive OS project lock. Commit release manifests first and the index last, using the loaded index SHA-256 as a compare-and-swap token. Loading reports corruption, unsupported versions and index/release mismatches without repairing them. Corrupt or unsupported manifests must be explicitly quarantined before replacement. Recovery removes only idle, regular files matching `.covearchive-tmp-<32 lowercase hex>.part` exactly. Exact internal names are reserved by role: `archive-index.json` and `.covearchive.lock` in project directories, `release.json` in release directories, and exact temporary-pattern names in both. Other `.covearchive` prefixes and quarantine-like asset names are allowed.

`ensure_project` reuses only ASCII case differences in owner/repo project folders (GitHub names are ASCII), preserving established on-disk spelling without renaming; new folders use the requested spelling. A unique variant is reused; multiple case-variant siblings, possible in per-directory case-sensitive folders, raise `case_alias` even for an exact spelling. Non-ASCII names require exact spelling. 8.3 and non-case aliases are still rejected. Persisted spelling owns repository identity: callers adopt `state.index.repository` before committing, and commit identity checks remain exact.

`ProjectState.unindexed` exposes validated unindexed manifests for detect-only recovery, with the `unindexed_release` issue still present. These are untrusted claims: recovered `completed` and `local_sha256` fields prove nothing. Reconciliation requires `verify_asset` on actual bytes and re-committing release/index metadata through the normal writer/CAS flow. Loading writes, deletes, renames and quarantines nothing; it does not repair or rebuild the index. A missing index or an index that is a directory permits a recovery scan; a corrupt or unsupported index returns early without scanning.

Recovery requires exact repository equality with a valid index, including spelling: any case-only difference is reported as `consistency` and is not returned. When no valid index is loaded, recovered repository identity is an unverified claim; callers adopt the recovered manifests' spelling for reconciliation commits. A case-only different spelling cannot be re-committed through the exact identity checks. Corrupt/unsupported indexes still prevent scanning.

The recovery cap bounds retained eligible manifests: at most `MAX_RELEASES` parsed candidates plus the single iteration-local manifest. Retention follows sorted folder-name scan order, and results are sorted by release id. Duplicate ids consume no capacity: a later duplicate frees its earlier holder's slot for subsequent candidates, all matching folders receive `consistency`, and none receive `limit`. A unique candidate encountered at a full cap receives `limit` only if its id never becomes duplicate. Discarded manifests retain only ids/folders in memory.

A non-cooperating process can replace a manifest between validation and publication, and CoveArchive may overwrite that replacement without detecting the conflict. Unlink checks before and after publication are best-effort: they narrow the window, are not an atomic compare-and-replace and do not detect every foreign replacement. This is an accepted limitation for CA-S5.1; guaranteed compare-and-replace is not provided. Exclusive temps and no-replace asset publication continue to protect archived asset bytes.

| Guarantee class | Scope |
| --- | --- |
| Handle-anchored | On an authorized local NTFS root: pinned ancestors and relative handles reject traversal, reparse points, junctions, symlinks, directory swaps, 8.3 aliases and non-case aliases below the root; only owner/repo project folders reuse unique ASCII case variants with their established on-disk spelling; ambiguous siblings raise `case_alias` even for an exact request; non-ASCII names and other opens require exact spelling; exclusive temp creation; no-replace asset publication; size and hash from one read handle. |
| Best-effort / not guaranteed | Components above the root resolve once before its identity is pinned. Remote and non-NTFS volumes are rejected where detectable. `FlushFileBuffers` covers files only, with no directory-flush or power-loss guarantee. Held manifests deny in-place writes. A non-cooperating process can replace a manifest between validation and publication, and CoveArchive may overwrite that replacement without detecting the conflict. Unlink checks before and after publication are best-effort, narrow the window, are not an atomic compare-and-replace and do not detect every foreign replacement. This is an accepted limitation for CA-S5.1; guaranteed compare-and-replace is not provided. Other same-user/admin processes or antivirus may alter closed files, so completion is point-in-time verification. Verification rejects hard links (link count must be one), but later link creation cannot be prevented. |

**Known limitations carried forward (unresolved in CA-S5.1)**

- F3: the store does not bind a project directory to a repository identity. Foreign repository metadata under another project's directory loads without an issue; the planner rejects a mismatched index.
- L2: a planned record with no digest verifies on size alone, and `completed_record` accepts it; the planner routes such records to download.
- CA-S5.1 limitation: recovery reconciliation must adopt the persisted repository spelling; case-only different spelling is rejected by exact commit identity checks, even though it identifies the same GitHub repository.
- Informational: newer-schema manifests are quarantined only by an explicit call; the planner's `digest_added` path never compares a new publisher digest with the local hash; a remote asset named like a temporary-file pattern passes the planner but can never be written.

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

## CA-S6: managed aria2 transfer engine

The standalone backend owns an unprivileged aria2 1.37.x child: authenticated loopback-only RPC, TCP-table PID ownership proof, and a secret only in memory and an ACL-verified private config, never arguments. Its minimal environment strips proxies; no proxy support. Defaults: 2 active, 4 connections, unlimited bandwidth.
Strict `.part` names stay in work_dir: no overwrite, automatic rename, transfer-data deletion, archive publication or manifest writes. Only the final work_dir component is checked for reparse points; CA-S7's verified staging directory owns ancestry.
Explicit resume requires expected size and regular data/control files. aria2 cannot detect same-size changed content or tampered/truncated partials. If-Range helps; CA-S7 MUST fully hash every completed download. Completion is transport only, never local/publisher verification.
aria2 follows cross-origin redirects; no redirect-host enforcement is claimed. CA-S7 must probe with its own no-follow client, submit the approved URL, audit `currentUri`, and separately probe Retry-After (unavailable through aria2).
No engine retries/backoff. CA-S8 owns optional 1-5 s start jitter (default off), server cooldowns, bounded exponential backoff with jitter, and no bot-detection evasion.
Queue persistence, GUI integration, secure URL resolution, hashing and archive publication remain FUTURE. CA-S5.1 carryovers F3, L2 and F1 are untouched and NOT fixed. Real disk-full is not verified; real file-I/O is.
Tests may set the test-only `COVEARCHIVE_TEST_ARIA2` absolute executable override. Hard-killed parents can leave an ACL-protected private config after the child exits; stale-directory sweeping is FUTURE work.

## Development

Python 3.12 or newer is required. The test extra declares pytest as a development
dependency. Tests run offline and do not contact GitHub.

From the repository root, run:

```console
python -m pytest
```

The source package lives under `src/`; tests live under `tests/`.
The project uses the MIT license; see `LICENSE` for its terms.
