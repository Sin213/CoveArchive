# CoveArchive roadmap

**Never automatically delete archived files, historical releases, Git snapshots, or preserved Git refs.**

## Approved register

| Area | Feature priorities |
| --- | --- |
| CA-N1 Archive Health | Repository health monitoring — Very high; Latest vs. archived versions — High; Check All / Update All — High; Archive integrity audit — High. |
| CA-N2 Scheduled Preservation | Backups while the application is closed — Very high. |
| CA-N3 Git Preservation | Incremental full Git mirrors — Very high; Repository relocation history — Medium. |
| CA-N4 Smarter Downloads | Pinned versions and remembered asset selections — Medium; Source-only repository fallback — Medium; Interrupted-download resume — Medium. |
| CA-N5 Extended Providers | Self-hosted Forgejo/Gitea/GitLab — Later. |

## Numbered slices

| Slice | Scope | Dependencies | State |
| --- | --- | --- | --- |
| CA-N1a | Read-only archive health audit (foundation) | — | COMPLETE |
| CA-N1b | Repository health/availability checks and latest-vs-archived comparison | N1a | COMPLETE |
| CA-N1c | Health Dashboard + Check All/Update All | N1a, N1b | IN PROGRESS |
| CA-N2a | Scheduled preservation runner | N1a | PLANNED |
| CA-N3a | Incremental Git mirrors | — | PLANNED |
| CA-N3b | Repository relocation history | N3a | PLANNED |
| CA-N4a | Pinned versions + remembered asset selections | — | PLANNED |
| CA-N4b | Source-only fallback | — | PLANNED |
| CA-N4c | Interrupted-download resume | — | PLANNED |
| CA-N5a | Self-hosted providers | — | PLANNED |

## N1a foundation contract

`covearchive.archive.health.audit_project(root_path, repository, limits)` returns
immutable evidence for one explicitly selected project, deterministic when the
audit is not truncated. Only
completed index records with consistent release metadata can become verified,
after hashing actual bytes. Unindexed metadata remains untrusted; its asset
count is reported and its assets are never hashed. Planned records and leftover
temporary entries indicate incomplete state and are left untouched.
An existing empty archive/project is a valid healthy result; a missing selected
project in a populated archive is reported as missing.

Stable codes distinguish verified, missing, damaged, unsafe, unverified, and
untrusted observations. Reports include category counts before finding capping,
verified assets, applied limits, and coverage/truncation flags. Aggregate limit
findings identify skipped asset/release counts where known. Bounds cover release
directories, indexed asset attempts, individual and total asset bytes, total
metadata bytes, directory entries examined, and retained findings. Every bound
that skips work makes coverage incomplete; incomplete evidence cannot be healthy.

The synchronous API has no Qt, GUI, network, subprocess, scheduling, execution of
archived bytes, or repair/cleanup path. Case-aware project lookup uses a
non-blocking POSIX flock on parent directory handles; a held lock reports busy
without creating a lock file or waiting for its holder. The entry budget covers
ancestor case lookup and project/release scans. Bounded enumeration stops at the
remaining budget plus one overflow lookahead, using pinned-fd `os.scandir` on
Linux and single-entry native queries on Windows. No further enumeration occurs
after budget exhaustion. Only the bounded sample is sorted. Truncated samples
follow filesystem order and need not repeat identically; truncated lookup reports
a limit instead of claiming a missing or uniquely matched project. Default
unbounded enumeration and blocking POSIX lookup remain unchanged for other users
of the filesystem API. Windows sharing conflicts already fail busy immediately;
the audit adds no Windows advisory lock. Reading may update atime.
Audit file opens require a single link, bypassing POSIX recovery-sibling scans;
the default filesystem APIs retain recovery-alias behavior for existing callers.
Verification is evidence at a point in time: same-uid in-place TOCTOU races remain
possible. The shared filesystem abstraction supports Windows; native Windows
verification has not been performed for this slice.

## N1b detection contract

`covearchive.archive.availability.assess(repository, scan_result, report)` is a
pure, Qt-free status projection for one selected GitHub repository.
`check_repository(root_path, repository, ...)` combines a bounded anonymous scan
with the read-only N1a audit, including when upstream fails. It is synchronous
and must run off the GUI thread; N1c uses ScanController for hard deadlines.
Stable and prerelease lines rank timezone-aware publication dates independently.
Only matching release IDs, tags, asset IDs, sizes, and hashed bytes can establish
verified status; incomplete evidence cannot prove absence. Unsupported providers,
ambiguous listings, reused tags, transient errors, and unavailable archives have
explicit states. Prereleases never drive the headline. No downloads, archive
mutation, credentials, queue, Library, scheduling, or dashboard are introduced.

## N1c dashboard contract (in progress)

Library's Health view runs only on explicit Check Selected / Check All. Each
repository uses one anonymous, supervised `--health-worker` process for the N1b
scan and N1a read-only audit, with a hard deadline, cancellation and reaping.
The bounded v2 protocol sends safe status and asset identity/hash fields, never actionable catalogs,
download URLs, credentials, publisher titles/bodies or exception text. Results
and last-known release tags are session-only and provider-qualified. Check All
is sequential and stops at a rate limit; cancellation retains completed results.
Checks and review preparation never modify the Library, queue or archive.

Review Updates / Update All offers checkboxes only for confidently identified new
or partially archived releases with saved rules and a valid chosen destination.
Changed metadata, reused tags, damaged/missing files and inconclusive evidence
remain excluded, with explanations and Open in Browse. Queue Selected prepares
repositories sequentially using an anonymous supervised scan for current catalog
metadata followed by a fresh Health check. A second bounded read-only audit in
that Health child obtains verified asset IDs; if its evidence differs from the
first audit, preparation fails closed. Verified local assets are omitted.

Only after preparation does a separate confirmation allow ArchiveBridge queue
insertion, with saved format/filename rules and `requeue_completed=False`.
Identities, Library membership, current health evidence, rules and metadata are
revalidated; stale or expired preparations are skipped. Process deadlines and a
five-minute preparation budget bound work. Cancellation before confirmation
queues nothing; stopping submissions retains jobs already explicitly submitted.
Checks never enable Auto-Archive or repair/replace preserved data. N1c remains
IN PROGRESS pending final review and validation; no later slice is started.

## Feature references

Git-Sync and Salvage are feature references only, not code dependencies.
Git-Sync has no verified reuse license and its source code must not be copied.
