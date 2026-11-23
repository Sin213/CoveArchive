# Scheduled preservation (CA-N2a)

CoveArchive can check opted-in GitHub repositories while its window is closed.
Scheduling is off until you explicitly enable it. It never deletes archived
files, historical releases, Git snapshots or preserved Git refs.

1. In Browse, scan each repository, select formats/prereleases/filename filters,
   and press **Save rules for this repository**. Select an existing writable
   archive folder and archive something once to save that destination.
2. In Library, enable **Auto-Archive** for each wanted GitHub repository and choose
   its interval (1, 6, 12 or 24 hours). Other providers are not scheduled.
3. Open Settings → **Scheduled preservation** → **Enable**. Use **Check status**
   to query the OS scheduler. The group shows On/Off, last completed invocation,
   next eligible check and last error. Opening Settings does not query or register
   OS tasks. Scheduler operations run off the GUI thread, with command timeouts.
4. **Disable** removes only this feature's scheduler registration. It stops its
   active systemd service, if any. It leaves Library, saved rules, queue jobs and
   every archived file intact. Removing a repository's Auto-Archive choice also
   stops subsequent scheduled discovery for that repository.

For a source checkout, run `python run_covearchive.py --scheduled-run` for one
headless invocation. Installed executables accept `--scheduled-run` directly.
The scheduler uses absolute argv, no shell, and a fixed application path. Keep
that installation/check-out and its Python environment available; after moving
it, disable/re-enable from the new location. AppImage extracted mount paths can
be temporary: automatic registration from such a mount is not portable; use a
stable extracted installation until a persistent AppImage launcher is supported.
Interpreter paths are made absolute and normalized without following symlinks,
so a source checkout's virtual-environment interpreter stays in that environment.
Worker children use the same preserved interpreter path. In systemd ExecStart,
the executable token retains literal `$`; arguments encode literal `$` as `$$`.
Both tokens encode `%` as `%%`, with ordinary double-quote/backslash escaping.
The lead verified executable `$` behavior on real systemd; executable `%` remains
a native verification gate. Windows commands remain argv lists plus escaped XML.

## What a run does

The runner reads the existing Library and preferences without changing them.
New state has its first check due immediately. Saved state retains individual
intervals across GUI closure and repeated OS wakes. It uses existing GitHub
discovery and Browse eligibility rules, `QueueStore.enqueue_many` with
`requeue_completed=False`, and the existing `QueueRunner` archive pipeline.
There is no second downloader or archive engine. With nothing due, it opens no
queue database and makes no network requests.

Jobs are deduplicated by existing queue identity. Completed jobs are not
downloaded again. Paused, cancelled and failed jobs are not manually retried by
this feature; use the Queue controls to resume/retry them. Already pending jobs
in the shared queue may run too, including work previously queued in the GUI.
Missing archive files behind completed queue records are not automatically
requeued; inspect Library Health and resolve them explicitly. Existing archive
verification, collision, naming and no-replace protections still apply. Reused
tags, changed metadata, modified content and conflicts cannot authorize replacing
preserved bytes.

Before any scheduled download worker starts, the existing read-only N1a Health
audit checks the reserved job's project while QueueRunner holds RunnerLock. This
applies equally to newly enqueued work and pre-existing pending GUI jobs. Missing
or corrupt historical manifests/indexes, changed or missing preserved bytes,
untrusted release metadata and incomplete audit coverage stop further queue
starts. The reservation returns to QUEUED with `needs_attention`, without
downloading or reconstructing metadata. The run records a non-success (exit 4)
and does not advance successful-check timing; Settings directs you to Health and
the GUI queue. Other checked repositories may be conservatively deferred too.
The affected opted-in repository is recorded even if its check was not due.
Inspect the damage and use the GUI explicitly; GUI repair behavior is unchanged.

Healthy/empty projects and genuinely absent new project directories are allowed.
Interrupted brand-new releases may continue only when their directories were
never indexed and contain no preserved files/metadata, only empty space or idle,
single-link application scratch temps. No historically indexed directory gets
this exception, even if all its files are now missing. Existing scratch cleanup
remains confined to that uncommitted new-download case; scheduled runs cannot
restore historical release.json or archive-index.json. Audits are point-in-time
evidence; changes by external writers after inspection remain an existing N1a
limitation. GUI and scheduled queue download owners remain mutually exclusive.

One lifetime lock excludes overlapping scheduled invocations. A competing
invocation reports `already_running` and changes no schedule or queue state.
The archive worker uses the existing `RunnerLock`; if a GUI runner owns it, new
jobs may be safely enqueued transactionally, but scheduled success is not
recorded. The GUI can consume those jobs. A later scheduled check sees the same
identities. Discovery alone is not successful preservation when downloads fail,
remain pending or are user-stopped. A complete discovery with no matching assets
is a successful check.

Runs are anonymous. They do not open/unlock a credential vault or reuse a token
from an interactive GUI session. GitHub's anonymous API allowance is lower, and
both discovery and queue metadata checks consume requests. Tokens are not put in
argv, scheduler files, state or logs. Reports use error categories, not upstream
payloads, URLs or exception messages.

## Timing, state and limits

The Linux entry is `covearchive-scheduled.service` plus
`covearchive-scheduled.timer`, in the user's systemd unit directory under the
existing XDG config base. Its calendar wake is every 15 minutes with
`Persistent=true`. It needs a working systemd user manager. No sudo or
`loginctl enable-linger` is used. Logging out may stop the user manager; CoveArchive
does not change that policy. Persistent catches missed wakes when the manager
returns, rather than preserving while the machine is off.
Registration refuses when either systemd unit name resolves outside the per-user config directory.

Windows uses one task per user, named
`\CoveArchive Scheduled Preservation <SID>`, where `<SID>` is the validated
current-user SID (for example `S-1-5-21-123`). Windows task names share a machine
namespace; including the SID prevents different accounts from sharing an entry.
Every query, creation and deletion targets that user's SID-named task as one
argv element. Before replacing or deleting an existing entry, CoveArchive checks
both its application description marker and principal UserId against that SID;
a mismatch returns an ownership error and leaves the entry untouched. An absent
entry is created without forced replacement; status/disable remain idempotent
and do not create tasks or files. Other accounts' and older unsuffixed entries
are never modified. The task wakes every 15 minutes with StartWhenAvailable and
IgnoreNew. Its current-user SID,
InteractiveToken and LeastPrivilege principal need no stored password. The user
must be logged in; locking the desktop is fine. Sleeping/offline machines,
missing removable disks and network outages delay preservation. Neither backend
promises an exact wall-clock archive time or wakes a sleeping machine. Next check
in Settings is a saved eligibility time, not a promise of an OS wake; the normal
additional delay is up to 15 minutes. It is refreshed by Check status/reopening
Settings.

`scheduled.json` lives beside the existing queue state (XDG_STATE_HOME/covearchive
on Linux; LOCALAPPDATA/CoveArchive on Windows). It has a strict version, field
allowlist, numeric/type/depth limits, at most 10,000 repositories and a 4 MiB cap.
Writes use a private temporary file, fsync and atomic replacement; new POSIX
directories/files have 0700/0600 permissions. Windows relies on the existing
per-user profile directory's inherited access controls, as other application
state does. It stores per-repository attempt/success/next due/failure count/error
category, a shared rate-limit deadline, and global start/finish/outcome/error.
Unreadable/malformed state is ignored, rebuilt with safe defaults, and reported
as `state_invalid`; nothing is moved/deleted and Library/preferences are untouched.
Unwritable state stops the invocation. Invalid Library/preferences fail closed.
Clock rollback clamps deadlines to at most one interval; implausible future
deadlines fall back to due checks. A crash leaves incomplete checks due on restart.

Transient scan/queue availability failures use the existing Auto-Archive retry
ladder (5, 15, 30 minutes, then the ordinary interval). Rate-limit hints are
bounded to 1–60 minutes and block other discoveries until their deadline.
If discovery of any repository hits a rate limit, no queue network work starts
in that invocation, including for jobs enqueued by earlier discoveries. Those
jobs remain pending, successful timing is not advanced, and the cooldown is
recorded for the next invocation.
Download failures retain queue retry/cooldown behavior. A run normally lasts at
most 15 minutes, with 2 minutes per discovery and 10 seconds of shutdown grace
plus a bounded child reap. It does not wait indefinitely for queue retries.
SIGINT/SIGTERM and Windows CTRL_BREAK where delivered request orderly shutdown;
unresponsive children are killed and reaped. Closing a Windows console or Task
Scheduler forcibly ending a task may prevent a final state write. A worker also
observes parent-pipe EOF and has its own runtime watchdog. Forced termination
leaves queue claims recoverable on the next owned run. OS/filesystem stalls can
still exceed application timeouts.

Exit codes: 0 clean (including not opted in, nothing due, already running);
2 missing/invalid saved inputs; 3 discovery failure; 4 queue/download failure or
user-stopped matching jobs; 5 interrupted; 6 state unavailable/rebuilt after
invalid input; 7 global deadline. State and a small JSON stdout report distinguish
outcomes. Windowed builds may have no console output; durable state is primary.

## Verification and native Windows/frozen handoff

Offline tests inject command runners; they never register real tasks or contact
GitHub. `tests/scheduled_fixtures.py` is an import-only fake GitHub/transport seam
used by subprocess tests; production argv cannot select it. Real QueueRunner and
archive filesystem writes are exercised. There is no download redirection
environment variable or fixture-loading production flag.

Run from the checkout with installed test dependencies:

```sh
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_scheduled.py tests/test_scheduled_process.py tests/test_scheduler.py tests/test_ui_scheduled.py
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

The authoring sandbox cannot access the systemd user bus. The lead's isolated
runtime-link smoke script is `/tmp/sinflow-v37-1000-bu4fem25/systemd_smoke.py`:

```sh
python /tmp/sinflow-v37-1000-bu4fem25/systemd_smoke.py
```

It uses unique unit names, runtime links, synthetic repositories, temporary
config/state/archive directories and fake network I/O, then stops and removes
only its own links in a `finally` block. It never enables persistent scheduling
on the main profile. Keep its resulting JSON/hash evidence for review.

**Packaging limitation.** Only the Windows onedir/installed build can register a task. Portable onefile
refuses registration with `portable_build_unsupported` because Task Scheduler
End can orphan the onefile Python child. Status and Disable remain available.
Onefile Task Scheduler End behaviour remains **UNVERIFIED/unsupported**.

**Windows has not been natively verified by the Arch author.** In a disposable
Windows user profile, run the offline focused tests and full suite first. Then:

1. Use a source checkout in a path containing spaces and Unicode. For OS task
   checks, use this disposable Windows account's default LOCALAPPDATA so the GUI
   and scheduled task see the same settings; reserve environment overrides for
   direct one-shot/frozen tests. Different LOCALAPPDATA directories do not change
   the scheduler SID. Configure a disposable archive, saved rules and Auto-Archive repositories.
   Do not use real archive/history folders. In Settings, confirm opening does
   not create a task; Enable, Check status, Enable again, Disable twice. Inspect
   Task Scheduler's exported XML: one SID-named task for this user, matching
   principal UserId, InteractiveToken,
   LeastPrivilege, 15-minute repetition, StartWhenAvailable, IgnoreNew, correctly
   quoted source argv, and no credentials. Verify no password/admin prompt.
   To inspect the exact entry in PowerShell in this isolated profile:

   ```powershell
   $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
   $taskName = "\CoveArchive Scheduled Preservation $sid"
   schtasks /Query /TN $taskName /XML
   ```

   Test two isolated Windows accounts: each must see/control its own entry and
   leave the other's XML unchanged. A foreign description or principal at the
   computed name must block replacement/deletion with an ownership error.
2. Re-enable from that profile and use Task Scheduler **Run** once while logged
   in with the GUI closed. Use a disposable public GitHub repository with small
   assets for this explicit native check, or run the subprocess fixture tests
   separately for deterministic offline preservation. Record LastTaskResult,
   scheduled.json, queue states, process exit, archive file lists and SHA-256
   before/after. Run again before due and after making it due; prove no repeated
   completed downloads. Add a release and verify only the new asset is fetched.
   Keep historical paths/hashes unchanged. Test logged-in locked desktop and
   missing destination, then Disable and remove only the disposable data. If
   manual task cleanup is needed, first verify the exported description is
   `CoveArchive scheduled preservation` and its principal UserId equals `$sid`,
   then use `schtasks /Delete /TN $taskName /F` for that verified entry only.
3. With a GUI queue run holding RunnerLock, start a scheduled run; confirm it
   defers without success and keeps jobs intact. Test two simultaneous scheduled
   runs, CTRL_BREAK, Task Scheduler End, and restart after a killed process.
   Verify there are no live workers after cleanup and paused/cancelled jobs stay
   stopped. The task is intended for a logged-in user, not logged-out execution.
4. Build the existing PyInstaller **onedir and onefile** Windows packages without
   changing versions/dependencies. In a fresh isolated LOCALAPPDATA, run each
   actual executable with `--scheduled-run` before Qt loads: first with no opt-in,
   then the disposable opted-in fixture. Confirm parent/scan/queue worker modes
   use the same executable, inherited pipes work despite `sys.stdin/stdout=None`,
   onefile extraction is reused safely by children, shutdown leaves no workers,
   and no Qt platform/display initialization is needed. Verify task registration
   for **onedir only**, invoking the stable EXE path plus `--scheduled-run`.
   For onefile, verify registration refuses with `portable_build_unsupported`,
   no task is created, and Disable still removes an existing owned task.
   Onefile Task Scheduler End remains UNVERIFIED/unsupported. Re-test package
   selftest and existing packaging verification. Keep native logs; mocked argv
   and XML tests on Arch do not establish native Task Scheduler/frozen success.
