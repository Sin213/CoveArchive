# CoveArchive

CoveArchive is a project for local preservation of GitHub project releases.
Its intended purpose is to help people keep release assets in a destination
they control, with an index that can support later verification and rescanning.

## Status

**pre-alpha, slice CA-S9A** - GUI archive workflow and saved Smart Build Filters.

The earlier pre-alpha, slice CA-S3 delivered project bootstrap, a GitHub URL classifier,
and a release-asset format matcher. CA-S7 adds the selected-asset archive backend.
The URL parser classifies supported targets, and the format matcher identifies
supported asset suffixes. An offline test suite covers both components.

The GUI uses CA-S2 release discovery through the official GitHub REST API. Expand
releases to inspect all assets; format and prerelease filters run locally. Release
notes are not shown. Browse selects builds and destinations; Queue controls the persistent
SQLite archive queue through the production CA-S7 pipeline.

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
that might otherwise leave a grandchild behind. Frozen packaging is not validated here.

## Linux source launch

On Arch Linux, run `python run_covearchive.py` with the dependencies above in a
native Wayland or X11 session. Choose an existing absolute native archive root
(for example `/home/user/Archives`), then scan, select assets, enqueue them and
explicitly start the queue. Reopening the app recovers queue state without
starting downloads automatically.

Preferences use `$XDG_CONFIG_HOME/covearchive` (default `~/.config/covearchive`);
the queue uses `$XDG_STATE_HOME/covearchive` (default `~/.local/state/covearchive`).
Relative XDG values are ignored. New archive files have mode 0600;
new archive and application directories have mode 0700.
The Linux backend uses pinned directory descriptors, no-follow opens, `flock`
ownership and fd-bound no-replace binary publication through
`linkat(AT_SYMLINK_FOLLOW)` and `/proc/self/fd`. A hardlink-capable local filesystem
and accessible proc fd links are required; unsupported publication fails closed.
A crash between linking the final name and removing the reserved temp alias
is recovered on the next run. File contents are fsynced before publication;
directory fsync is best effort, with no power-loss durability promise.
Linux has no handle-based unlink: a same-uid replacement after the final identity
check may be unlinked by temp cleanup. Metadata rename/replace retains same-uid pathname
races. Such writers can modify closed files; publication does not make files immutable.
The separate aria2 engine remains Windows-only; selected-asset archiving uses
the shared streaming resolver. `python run_covearchive.py --smoke-test` uses
temporary XDG state and opens and closes the GUI.

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
- `.exe` - EXE (opt-in; archived only, never launched or installed)
- `.msi` - MSI (opt-in; archived only, never launched or installed)

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
**Limits:** CA-S4 remains pure in-memory planning and metadata validation. CA-S5 provides local persistence; CA-S7 connects downloads to it. GUI integration remains future work.

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

CA-S8 queue defaults and limitations are documented below; no bot-detection evasion or circumvention of GitHub restrictions is provided.

## GUI archive workflow and saved build filters

Use Browse to discover releases, preview matches, check builds and choose a local root.
Select Matching checks eligible matches; Clear Selection clears checks. Archive Selected queues only checked AND currently eligible assets; Queue shows progress.

1. Paste a GitHub repository or releases URL.
2. Discover releases through the official GitHub REST API.
3. Filter release assets by supported format.
4. Choose a local destination.
5. Download selected assets into per-release folders.
6. Verify downloaded files and maintain an index.
7. Resume interrupted work.
8. Rescan for new releases without duplicating existing downloads.

Smart Build Filters use comma-separated shell wildcards, matched ASCII case-insensitively.
Includes are OR (empty means all); any exclude wins. `[,]` matches a literal comma.
`*win*` also matches darwin: inspect the preview. Save rules explicitly per repository.
Preferences live at `%LOCALAPPDATA%\CoveArchive\preferences.json`; corrupt/newer files are preserved and read-only.
Pause stops pending jobs or requests a cooperative stop; Cancel stops work; Retry re-queues failed/cancelled jobs.
Resume restarts an incomplete file from the beginning. Rate-limit waits happen automatically; Resume preserves attempts/cooldowns.
Completion comes from the backend result. Queue completion is historical; files are re-verified only when re-queued.
Pause and Close requests durable pause and waits for workers and the queue lock. Slow shutdown honestly reports still-active work.
Opening the app never starts restored jobs: press Start Queue explicitly.

CoveArchive is not a complete Git history backup.
The workflow preserves selected release assets; it does not promise
to mirror every part of a GitHub project.

As a design rule, downloads are never automatically executed or extracted.

## CA-S6: managed aria2 transfer engine

The standalone backend owns an unprivileged aria2 1.37.x child: authenticated loopback-only RPC, TCP-table PID ownership proof, and a secret only in memory and an ACL-verified private config, never arguments. Its minimal environment strips proxies; no proxy support. Defaults: 2 active, 4 connections, unlimited bandwidth.
Strict `.part` names stay in work_dir: no overwrite, automatic rename, transfer-data deletion, archive publication or manifest writes. Only the final work_dir component is checked for reparse points; CA-S7's verified staging directory owns ancestry.
Explicit resume requires expected size and regular data/control files. aria2 cannot detect same-size changed content or tampered/truncated partials. If-Range helps; CA-S7 MUST fully hash every completed download. Completion is transport only, never local/publisher verification.
aria2 follows cross-origin redirects; no redirect-host enforcement is claimed. CA-S7 uses a separate redirect-controlled streaming backend; aria2 is excluded from its production path because actual transfer redirects cannot be constrained.
No engine retries/backoff. CA-S8 owns server cooldowns and bounded exponential backoff with jitter; start delay is deferred to CA-S9.
Queue persistence and GUI download integration are available. CA-S7 supplies secure URL resolution, hashing and archive publication; its treatment of the unchanged CA-S5.1 F1/F3/L2 carryovers is described below. Disk-full injection is tested; actual disk exhaustion is not verified.
Tests may set the test-only `COVEARCHIVE_TEST_ARIA2` absolute executable override. Hard-killed parents can leave an ACL-protected private config after the child exits; stale-directory sweeping is FUTURE work.

## CA-S7: secure download-to-archive pipeline

`archive_asset(repository, release, asset, archive_root, ...)` handles one selected
uploaded asset: pure planning, fresh API resolution, bounded streaming to an
exclusive opaque NTFS temp, size/SHA-256 verification, no-replace publication,
then release.json followed by archive-index.json. Assets remain in per-release folders.
Anonymous requests only: private repositories are unsupported. CA-S8 supplies the queue
backend and GUI download controls. A scheduler daemon, background service or tray agent is
not implemented; opt-in checks while the window is open are described in CA-A1: Auto-Archive below.

Production uses HTTPS with TLS verification, no credentials/cookies, no environment
proxies/netrc/CA overrides, no automatic redirects, and identity byte encoding.
The identity-bound API may return binary bytes directly with 200 or redirect with 302;
both paths receive the same header, peer, size, and streaming checks.
Each actual redirect hop is checked independently, with loop/hop limits and an
exact hostname allowlist: `api.github.com` is the identity-bound asset API;
`release-assets.githubusercontent.com` is the current signed release CDN;
`objects.githubusercontent.com` and `github-releases.githubusercontent.com` are
established release storage hosts. These documented/observed hosts must be
re-verified when GitHub changes; unknown destinations fail closed, with no wildcard.
Caller-supplied browser URLs are never trusted. Signed URLs stay transient and a
rejected signed URL is re-resolved once. Errors/results/manifests omit signed URLs;
connection-pool DEBUG logging is suppressed only for the transfer thread during I/O.

DNS answers must be public; connected-peer checking is best effort when a socket
is exposed. Requests resolves again, leaving a DNS-rebinding TOCTOU window: this
is not DNS pinning. aria2 remains available separately and regression-tested;
it follows redirects, so preflight validation cannot enforce its later destination.
CA-S7 therefore uses one in-process streaming connection, no range resume, and
restart-from-zero on retry. Stalls have bounded socket timeouts; the overall default
budget is six hours. Partial reads return control between arrivals; cancellation latency
is bounded by the per-socket read timeout. urllib3 1.x without read1 uses bounded
4096-byte reads; deadline/cancel checks have coarser granularity until each read returns.
Retry waits respect the remaining deadline.
Read/connect/server retries are bounded before body consumption;
mid-body failures return partial_transfer. Rate-limit guidance is returned to callers.

`ArchiveResult.status` is ARCHIVED, ALREADY_ARCHIVED, VERIFIED_EXISTING, MODIFIED,
CONFLICT, MISMATCH, CANCELLED, UPSTREAM_REMOVED, RECONCILE_REQUIRED, or FAILED.
PUBLISHER_VERIFIED maps to AssetRecord.publisher_verified; LOCAL_HASH_ONLY means a
completed record with a real local hash and no publisher digest. JSON alone is no proof.
A newly available publisher digest is checked against completed bytes without rewriting
the recorded entry: a matching skip keeps its recorded integrity label. A conflicting
digest fails before a missing completed file is fetched. Planned/recovered records enforce
and record the effective publisher digest before claiming PUBLISHER_VERIFIED.
Completed files are rehashed before skipping; modified files are preserved. Missing
files are downloaded again and checked against prior hashes. Planned files with a
publisher hash may be adopted after verification; without one, fresh matching bytes
are required. Changed recorded metadata requests reconciliation. Recovery downgrades
unindexed claims to planned and re-establishes proof. Remote removal never deletes files.

Each asset commit briefly holds the per-project lock, reloads and merges current
records, then commits release first and index last with CAS. Unrelated indexed damage
may prevent index commit after publication: manifest_commit_incomplete preserves the
file. A narrowly validated release-ahead-of-index interruption can converge: unchanged
index assets win, additional manifest claims are downgraded to planned, and selected
bytes are re-established before both documents are committed. Recovery also permits
planned-to-completed transitions with the same publisher digest or a newly added one;
the index version wins and current metadata must supply any added digest again. Other manifest differences
remain fail-closed. A missing indexed release.json is restorable only when its folder
contains indexed asset names and exact internal temp names, with normal type/reparse
checks and orphan cleanup. The index supplies all records; verified existing bytes
skip network access but still rebuild release.json and commit the index (MANIFEST_RECOVERY).
Before transferring a new asset during restoration, a short writer lock rebuilds the
release manifest from indexed claims; an unchanged index is not rewritten. Only this
call's validated plan-allocated filename may extend the restoration name allowance,
with normal file/type/reparse checks; it supplies no integrity evidence.
An absent folder or interrupted restoration follows the same retry path. Foreign entries
block restoration. Unrelated commits may stay incomplete until the affected release
is reconciled; selecting a verified indexed sibling also commits pending recovery
without network access, retaining downgraded claims even if they vanished upstream. Existing-file hashing in the store is not interruptible; cancellation is
honored after adoption verification and before metadata commits. Only a file newly
published by this call starts the commit phase that completes despite cancellation.
Cancellation is honored through the last stream read/EOF check; a request during the
final flush/rename or later commit is treated as after publication.
Empty directories may remain after failure; partial temp files are cleaned up.
F1 best-effort manifest replacement and power-loss limits remain. F3 identity checks
are closed in this pipeline only; the store is unchanged. The store's L2 size-only
planned-record path is never used to authorize completion. Neither transport completion
nor a local hash alone implies publisher verification.

## CA-S8: persistent archive queue

`covearchive.queue` exposes `QueueStore` and `QueueRunner`; callers drive `run_pending()`.
`QueueStore()` uses `%LOCALAPPDATA%\CoveArchive\queue.sqlite3`; `QueueStore(path)` selects an explicit file.
Schema version 1 uses rollback journal `DELETE`, `synchronous=FULL`, and no WAL.
The validated Windows runtime uses SQLite 3.49.1; SQLite is supplied by Python, not bundled separately here.
States and allowed transitions (controls/claim/finish enforce ownership as appropriate):

| From | To |
| --- | --- |
| QUEUED, RETRY_WAIT | RUNNING, PAUSED, CANCELLED |
| RUNNING | COMPLETED, RETRY_WAIT, FAILED, PAUSED, CANCELLED, QUEUED |
| PAUSED | QUEUED, CANCELLED |
| COMPLETED, FAILED, CANCELLED | QUEUED |

Identity combines GitHub repository key, release/asset IDs and normalized archive root.
Aliases that string normalization cannot canonicalize (8.3 short names, junctions, trailing dots/spaces) rely on CA-S5's project lock and may fail as retryable busy (automatic retry capped at one).
Active duplicates reuse a row; COMPLETED re-enqueue queues CA-S7 re-verification.
FAILED/CANCELLED duplicates stay unchanged until explicit `retry()` resets the attempt count.
Claims are atomic FIFO transactions, with at most one RUNNING job per project/root.
Defaults allow two active workers. RunnerLock permits one runner per queue file;
the OS releases it at process death. Archive files are never modified by queue storage.
`pause()` stops pending jobs immediately and requests cooperative stop of owned active jobs.
`resume()` queues PAUSED jobs, preserving cooldown; `cancel()` requests cancellation.
`retry()` queues FAILED/CANCELLED jobs. A successful archive result wins a racing stop request.
Active pause/cancel intent is persisted before signaling and honored at restart without consuming an attempt.
Defaults allow five attempts: 5-300 s exponential backoff plus capped jitter;
Retry-After is honored exactly. Permanent codes are never retried; `disk_full` requires explicit retry.
Catalog and asset requests share a persisted queue-wide GitHub cooldown; claims pause until expiry, exposed by `snapshot().cooldown_until`.
The download path honors X-RateLimit-Reset when Remaining is zero, using the larger header wait; 60 s is the fallback only when neither usable header is present.
Receiving a 429 and committing its cooldown are not crash-atomic; daemon workers may outlive shutdown, and wall-clock changes can shorten or lengthen cooldowns.
`manifest_commit_incomplete` can mask disk-full/permission failures and is retried automatically at most once.
Runner startup recovers interrupted RUNNING rows; SQLite recovers hot rollback journals.
Each interrupted run consumes one attempt; exhausting the attempt limit leaves FAILED with `interrupted`.
CA-S7 always re-verifies archive truth; queue COMPLETED is never proof of intact bytes.
Progress and measured speed remain in memory; retry scheduling uses wall-clock timestamps.
Clock rewind can delay retries. Restarted incomplete transfers start from byte zero: no byte-level resume.
Bandwidth throttling and start delay remain deferred. A scheduler daemon, background
service or tray agent is not implemented. See CA-A1: Auto-Archive below for opt-in monitoring
while the window is open.
CA-S2 GitHub catalog discovery is not cancellable: controls/shutdown take effect after it returns.
Final CA-S7 filesystem phases are not cancellable. An uncommitted published file without
a publisher digest is downloaded again by CA-S7 to establish proof.
Shutdown joins daemon workers cooperatively and may report unresolved job IDs;
the queue lock stays held until workers and the run loop finish. No forced thread kill is used.
After a shutdown timeout, the last finished worker releases the lock automatically once the run loop has exited.
Crash tests demonstrate process-restart behavior, not power-loss durability.

## Development

Python 3.12 or newer is required. The test extra declares pytest as a development
dependency. Tests run offline and do not contact GitHub.

From the repository root, run:

```console
python -m pytest
```

The source package lives under `src/`; tests live under `tests/`.
The project uses the MIT license; see `LICENSE` for its terms.

## CA-S9B: Library (collections and bulk management)

The third tab organizes repository metadata. Library actions never move/rename/delete
archived files or edit Smart Build Filters. Auto-Archive is the single opt-in exception to
metadata-only actions: after the user enables it, checks run only while CoveArchive is open
and enqueue matching assets only through the existing archive queue. Other Library actions
never start scans or downloads or change the archive queue.
Remove from list removes tracking metadata only. Friendly names do not change repository identity.
Collections nest up to 8 levels; deleting one keeps its repositories and assigns them to its parent
(or Unsorted at the top level). Collection rename/move operations change metadata only.
Selecting a collection browses all descendants; search combines that scope with names,
repository references, collection paths and providers.

Add a URL, use Bulk Add..., or press Ctrl+V outside a text field. Results report added,
duplicate-skipped and failed counts. GitHub identity is provider + case-insensitive owner/repo:
`.git`, `/releases`, `www.github.com`, trailing slash and query/fragment variants deduplicate.
New repositories enter the selected collection, or Unsorted from All repositories/Unsorted.

Library data is `library.json` beside `preferences.json`: `%LOCALAPPDATA%\CoveArchive` on Windows,
`$XDG_CONFIG_HOME/covearchive` on Linux, defaulting to `~/.config/covearchive` (relative XDG ignored).
Loading creates nothing; library data is written only after a user change. Corrupt or newer-version
library files remain untouched and the Library becomes read-only. Limits are 10,000 repositories,
2,000 collections and 8 MiB per file. The saved Smart Build Filters limit remains 500 repositories.

Import previews counts before committing; any invalid entry prevents the entire import.
Export writes the native versioned JSON format, including empty collections, for example:
```json
{"format":"covearchive-repository-list","version":1,"collections":["Games"],
 "repositories":[{"provider":"github","url":"https://github.com/Example/Project","name":null,"collection":"Games","folder":null}]}
```
Git-Sync import follows the supplied schema: `format: "Git-Sync data"`, integer `version: 1`,
`exported` (ignored), `filters`, and `repos`. Each repo uses `name`, `site`, `api`, `repository`,
`folder`, `filter`, and `keepPreviousVersions`. `repository` is a path, never a URL; `site` is a
bare host (optional port) or HTTP(S) host without a path or credentials. GitHub defaults to
github.com; blank `api` is inferred from the host (unknown hosts become `other`). GitLab/Gitea,
unknown APIs and enterprise GitHub hosts are kept inactive (`other` for the latter two).
API tokens are case-insensitive; malformed tokens and null/non-string API/site values count as blank. Paths trim outer whitespace/slashes and reject URL syntax and controls.
Valid string paths in `filters` create even empty collections; malformed items are ignored.
Fixtures are fictional; no real export file was inspected. Credentials, tokens, history,
settings and unknown fields are ignored. `filter` is a collection path with trimmed segments,
not a Smart Build Filter; `folder` is descriptive text only. `keepPreviousVersions=false`
only produces a notice and never deletes anything; other retention values are ignored.

The shared application icon ships in `src/covearchive/assets/`. This Library slice was verified
on Windows; Arch shares the code but is not claimed verified by these Windows runs.

## CA-A1: Auto-Archive

Auto-Archive is off by default. Add, Bulk Add, import, export-then-import and removing
then re-adding a repository never enable it. On the Library tab, select repositories and
choose **Auto-Archive...**, then Off or an interval of 1, 6, 12 or 24 hours. Enabling requires
Smart Build Filters for each repository and an archive root that were saved successfully;
unsaved changes and failed saves do not qualify. Missing prerequisites
produce an actionable message without changing any watch. The consent dialog defaults to No.
Only active GitHub repositories can be enabled; GitLab scheduling is not supported.

Checks use a second scan worker process, the same GitHub discovery and saved filters, and
the existing durable queue. The first check queues every matching release asset not already
queued, potentially many assets. Existing jobs in any state, including COMPLETED, FAILED,
CANCELLED, QUEUED and RUNNING, are neither modified nor duplicated by automatic checks.
Queue startup follows the existing enqueue rules: restored or earlier pending jobs require
**Start Queue** on the Queue tab.
Queue jobs are per archive folder. Browse's Archive Selected saves the folder it used;
changing that saved folder makes the next check queue matching assets for the new folder.

Checks run only while the window is open, never at startup. Every launch re-arms a full
interval; a 24-hour watch requires the window to stay open for 24 hours. Check history is
kept only in memory. The Library's Auto-Archive and Last / next check columns and detail
line show status, last/next check times and actionable errors. The Last / next check column
has a full-text tooltip; the Auto-Archive tooltip includes the last result or actionable error.

Offline, timeout, server and queue-unavailable failures retry after 5, 15 and 30 minutes;
the fourth consecutive failure waits the regular interval. GitHub rate-limit hints are
clamped to 1-60 minutes and pause checks for all watches. Permanent errors wait the regular
interval and display an actionable message. Turning Auto-Archive off or removing a repository
stops future checks only: archives are never deleted and already queued work is not cancelled.

The version-1 `library.json` gains an optional `auto_archive` key, written only when enabled.
Older builds treat a library containing this key as unreadable and make it read-only.
Export/import do not carry the setting. Auto-Archive was verified on Windows only;
Arch shares the code but is not claimed verified by these Windows runs.

## Codeberg release archives

Browse accepts `https://codeberg.org/Owner/Repo`, its `/releases` URL, or the
same URLs without the scheme. Only public, anonymous access to codeberg.org is
supported; credentials, explicit ports, other forge hosts and tag URLs are rejected.
Attachment lookup and downloads allow codeberg.org only, with checked redirects.
Archives use `<root>/codeberg.org/<Owner>/<Repo>/<release folder>/`;
existing GitHub paths and metadata v1 remain unchanged. Codeberg metadata uses
v2, which older builds refuse rather than overwrite.
The queue upgrades transactionally to schema v2 while excluding an active runner.
Older builds lock the queue by the exact path given, so close any older CoveArchive before first launching this build (migration cannot detect an older runner using a different path alias).
Older builds refuse that queue file after upgrade: keep a backup if you may downgrade.
Restored jobs remain idle until Start Queue. Save Rules is disabled for Codeberg;
Library and Auto-Archive remain GitHub-only. Codeberg provides no attachment
digests: local SHA-256 is recorded, without publisher verification.
Only native attachments with known sizes are eligible; external links and unknown
sizes are excluded. Listings fail closed on pagination inconsistencies or limits;
renamed repositories and download hosts beyond codeberg.org are unsupported.
Attachment lists come from release listings (equal to the per-release assets endpoint for 221 live releases, maximum 21 assets; larger counts untested).
