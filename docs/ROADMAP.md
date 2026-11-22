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
| CA-N1b | Repository health/availability checks and latest-vs-archived comparison | N1a | IN PROGRESS |
| CA-N1c | Health Dashboard + Check All/Update All | N1a, N1b | PLANNED |
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

## Feature references

Git-Sync and Salvage are feature references only, not code dependencies.
Git-Sync has no verified reuse license and its source code must not be copied.
