# Incremental Git mirrors

In Browse, **Archive Git History** creates an immutable `history.bundle`
snapshot. **Update Git Mirror** incrementally preserves a persistent bare
repository. **Verify Mirror** checks its local structure, every capture's refs
and all object connectivity without contacting upstream. These manual actions
share cancellation and application-close supervision. Results show ref count,
last capture time, destination and safe error messages. Opening Browse, Library
or Health never downloads history. Scheduled mirrors are unavailable and OFF;
release Auto-Archive does not enable them (see the roadmap blocker).
Verify Mirror resolves the entered repository/releases URL locally and works
after a fresh offline start without a successful release scan. It starts only
on an explicit click; an unavailable upstream cannot disable local verification.

After a complete scan finds zero published releases, Browse shows **Source-only
repository** with the same manual archive and update actions. Set the archive
root and choose an action. Both actions require a URL that matches the scanned
repository. After a GitHub redirect, scan the current canonical URL. Browse
counts published releases, including prereleases, before applying filters.

Storage is `<project>/git-history/mirror/`: `repository.git/` is a bare reftable
repository, `identity.json` binds the canonical provider/host/project, and
`captures/capture-<uuid>.json` is each successful capture's immutable commit
marker. Project paths use the existing provider-aware naming rules. Case-only
ASCII aliases reuse their project; unrelated identities never merge.

Each JSON record contains original branch/tag names and object IDs, default
symbolic HEAD (or null), capture time and a refs digest. Ref
`refs/heads/main`, for example, is retained internally as
`refs/covearchive/captures/<uuid>/heads/main`. Each fetch targets a new namespace
without force or prune. Fetch admits an explicit set of advertised source refs;
ref additions racing that fetch are captured by the next update. Reftable stores names as data, preserving Windows
reserved names and case-colliding names without loose ref files. Git 2.45+
with reftable support is required, including when restoring those names.

Unchanged advertised refs and HEAD cause no fetch. Changed refs negotiate
against objects reachable from every previous capture. Successful captures
require strict object checking and publication through the existing atomic
archive filesystem adapter. Interrupted fetches/publication may retain extra
objects, orphan retention namespaces or owned metadata temporaries. Orphan refs
and objects never count as successful captures and are never pruned. Idle owned
metadata temporaries use the existing adapter's safe recovery. Retry creates
another namespace. A remote with no advertised refs has no initial successful capture;
if it becomes empty later, that empty state is recorded without losing history.

Initialization resolves Git and checks its version before publishing `mirror/`.
The initial repository is prepared in a private sibling and renamed into place
only when complete. Missing/unsupported Git, low disk space and initialization
failures remain retryable. An empty owned directory left by an older failed
initialization is reusable; preserved data is never deleted to retry.

Before any Git invocation on a reused mirror, validate the exact safe config,
fixed structural markers, identity, reftable paths and ownership/permissions
(POSIX), regular files and single links. Windows checks the tree owner and DACL
using ctypes/advapi32: only the current user, Administrators and SYSTEM may own
mirror data or receive write/delete/change-permission grants. Foreign read-only
and denied grants are harmless; NULL DACLs and foreign mutation grants fail
closed. Unsupported descriptors/API failure report that permissions could not
be checked. Ancestors within the archive root, repository/config, object files,
identity and capture metadata are checked before Git. Real Windows ACL behavior
remains an explicit item in the pending native Windows verification handoff.
Inherit-only CREATOR OWNER/GROUP ACEs are accepted as descendant-creation
placeholders. An effective CREATOR OWNER grant resolves to the already trusted
owner; an effective CREATOR GROUP mutation grant remains rejected. Actual foreign
mutation grants remain rejected, including inherit-only grants for real users.
POSIX ancestor validation extends from
the archive root through the mirror and checks system ancestors too: foreign
ownership and group/other-writable non-sticky directories fail closed. Trusted
system ancestors account for UID-mapped roots. Reftable names are enumerated
once, with entry bounds and cancellation/deadline checks during validation.
After trust validation, Git versions below 2.45 and unsupported reftable
capabilities report the existing upgrade-required error. Reject hooks, includes, worktrees,
alternates, symlinks/reparse points and external object directories. Every
operation uses the existing isolated Git runner, HTTPS-only production transport,
disabled hooks/credentials/submodules/maintenance, limits and repository lock.
POSIX Git subprocesses use umask 0077, independently of a permissive parent
umask, so Git-created object directories satisfy the same trust policy. Windows
process creation continues to use the existing NTFS/Job Object protections.
Verification fails closed; a damaged mirror is never deleted or recreated.
Mirror operations pin the validated archive root and export parent; pathname/handle identity mismatches fail closed.
POSIX identity/capture metadata and both history/mirror lock files use the same
owner/mode/type checks before recovery or content reads. All capture records are
checked even for a selected export. Files require one link; only a proven owned
record plus exact adapter-temp pair may briefly have two links during recovery,
after which reads require one link. External hardlinks and symlinks fail closed.
Owned sticky capture directories retain normal POSIX sticky-directory semantics.
Unavailable upstream is reported as inaccessible/not found, not proof of deletion.

Export using the Python API from the application environment (off the GUI thread):

```python
from covearchive.archive.githistory import export_mirror
from covearchive.github.urls import RepositoryTarget

bundle = export_mirror(RepositoryTarget("Owner", "Repo"), "/archive", "/exports/latest")
prior = export_mirror(RepositoryTarget("Owner", "Repo"), "/archive", "/exports/prior",
                      capture="capture-<uuid>.json")
```

Export rejects resolved destinations inside, equal to, or containing the source mirror (including symlink/junction aliases) before creating or modifying files or staging directories.
The export parent must exist; the destination must be new. Each export contains
`history.bundle` with original refs, `capture.json`, and `restore-head.txt`.
Restore offline:

```sh
git clone --bare --ref-format=reftable --branch main /exports/prior/history.bundle restored.git
git --git-dir=restored.git symbolic-ref HEAD "$(cat /exports/prior/restore-head.txt)"
git --git-dir=restored.git fsck --full --strict
git --git-dir=restored.git show refs/heads/main:path/to/file
```

Use the branch recorded in `capture.json`'s `head` field (without `refs/heads/`)
as `--branch`. Bundles carry HEAD's object ID, so explicit branch selection is
needed to distinguish two branches sharing the same commit. The post-clone
`symbolic-ref` step above restores the exact recorded branch. For a null HEAD,
omit `--branch` AND run that post-clone step: `restore-head.txt` names a generated
branch absent from the captured refs, making HEAD unborn even if Git initially
chooses an existing branch matching the local `init.defaultBranch` setting.
On Windows, read that file as UTF-8 and pass its trimmed text as the final
argument to `git --git-dir=restored.git symbolic-ref HEAD`.

An empty capture exports `empty.git/` instead of a bundle, since Git bundles
cannot encode an empty repository. `export_mirror` returns that cloneable path.
A missing upstream is never needed to export or restore. A null default HEAD
uses an unborn placeholder in the export staging repository; all original refs
remain exact.

The guarantee covers objects reachable from advertised branches and tags,
including annotated tag objects, and tips captured before subsequent rewrites
or deletion. Hidden/unadvertised server refs, external Git LFS payloads and
submodule repository contents are excluded. Repositories and retained capture
metadata grow permanently, bounded per operation by configured size/time/output
limits, including an entry cap and a 64 MiB aggregate capture-metadata cap.
Verification reads the existing object store even for unchanged checks.
Fetch checks byte and entry budgets during processing and again on completion.
An over-budget fetch never publishes a successful capture. Valid downloaded
objects and orphan retention refs remain preserved; verification/export use
independent read-work caps rather than the failed update's smaller budgets.
Their default hard caps are 64 GiB of combined repository/export working data
and four million repository entries, plus the existing output, metadata and
time limits. These read caps are configurable through `Limits`; exceeding a
hard read cap reports a work limit without deleting or recreating the mirror.
Full Verify Mirror includes all repository object bytes in its hard read cap
before fsck. Only export excludes unrelated retained object bytes from source
accounting; its selected bundle and scratch repository still obey the work cap.
Ref validation reads one capture namespace per Git call, with cancellation
checks between batches. It never emits all historical refs into one bounded
stdout buffer. Full Verify Mirror still checks every committed capture and
performs one object audit. Update preflight checks the latest namespace rather
than re-reading every namespace; explicit old-capture export reads that manifest
and namespace directly and validates its exported objects in private scratch.
Unrelated captures and failed-update object contents are excluded from that
export's content verification and scratch-size accounting.
Object-audit diagnostic buffers remain capped while excess stderr is drained;
Git's nonzero exit status still fails verification. Accumulating valid-history
warnings cannot exhaust access to captured data. Ref output and other commands
retain strict overflow handling; cancellation and deadlines still bound work.

Admission reserves stdout space for retained names, SHA-256-width IDs and export
diagnostics; it also bounds stdin refspec/update-ref data and ref names (1024
UTF-8 bytes, including HEAD command arguments). Oversized single captures fail
before fetch. Metadata bytes and the configurable capture-count capacity
(`Limits.max_captures`, default 10,000) are checked before ingestion, including
failed publication cases. Existing capture files remain readable when capacity
is reached. Byte/entry budgets reserve read/export headroom before an update;
incoming objects remain packed to bound entry growth. These capacity limits,
the metadata cap and batching bound full-audit work; existing subprocess and
overall deadlines and cancellation remain enforced. Increasing capacity is an
explicit Limits choice and must preserve the read-budget headroom.

Protected ownership/permissions make the initial full trust check stable for
an operation under its repository lock; new Git output is checked after fetch.
This avoids rewalking the entire object tree for each ref batch. Administrator
and concurrent same-user modification remain outside the trust boundary.
Local administrators and concurrent same-user file replacement are outside the
filesystem adapter's trust boundary. File fsync and atomic commit markers do
not promise power-loss durability. Ordinary cancellation/timeouts first try
graceful POSIX termination, then terminate and reap the process tree. While the
repository lock remains held, cleanup removes only newly created, unlisted Git
reftable lock/temp files compared with the pre-fetch listing. Pre-existing
files, listed ref tables and objects are retained. Failed fetches, including
size/entry-limit failures, also clean only newly created Git incoming/quarantine
directories, after bounded ownership/type/single-link validation. Pre-existing
directories, external aliases and migrated object files remain untouched.
Tests hold a genuine prepared
Git ref transaction on POSIX; Windows uses deterministic transient injection.
An application/host crash that prevents supervised cleanup can still leave
locks requiring manual inspection; unsafe/damaged structure fails closed without
automatic repair. Native Windows/frozen verification and independent review
remain release gates; no release is claimed by this implementation.
