# Repository relocation history

Select a Library repository and choose **Check repository location**. The check
runs anonymously in a separate deadline-supervised worker. GitHub's positive
numeric repository id anchors identity. Known ids resolve through
`GET /repositories/{id}`, independently of the saved URL. The saved URL is also
queried through `GET /repos/{owner}/{repo}` to distinguish an old URL occupied by
another repository. Redirects only route requests. Names, commits and shared Git
history cannot establish identity. Metadata fields must agree on the canonical URL.

| Outcome | Evidence |
| --- | --- |
| `UNCHANGED` | A verified location matches the reference in the check's pre-existing history, or this is the first observation of a new id. A first observation records **no prior identity**. |
| `RENAMED` / `TRANSFERRED` | The stable id verifies a different repository name or owner. |
| `URL_REUSED` | The saved URL resolves to a different id. The check warns and reports both ids. It omits the original id's destination if that location has a conflicting historical binding. It publishes no history or binding changes. |
| `UNAVAILABLE` | The saved URL and, when known, the stable id both return confirmed absence. |
| `INCONCLUSIVE` | Evidence is insufficient or conflicts, or the check cannot use the history store. A new alias that resolves to a tracked id returns `tracked_alias` after one lookup and writes nothing. |
| `UNSUPPORTED` | The provider is outside this public GitHub check. |

Only HTTP 404 and 410 count as confirmed absence. An anonymous 404 cannot
distinguish a missing repository from a private one. Access denial, an unreadable
403 response, rate limits, network failures, timeouts and invalid metadata yield
`INCONCLUSIVE` and authorize no relocation write. An earlier version-1 history
entry can establish a saved URL's identity when the URL has no explicit binding
and the recorded canonical location uniquely identifies it.

`relocation.json` beside `library.json` stores schema version 1, GitHub identity
records (`provider`, `host`, `repository_id`, ordered `locations` with canonical
`url` and `observed_at` epoch seconds), and URL observation bindings
(`url`, `repository_id`, `observed_at`, `no_prior_identity`). Removing Library
entries leaves their history intact. Repeated checks deduplicate the current
location, and returning to a previous location appends a new observation. The
4 MiB, 10,000 identity, 20,000 binding and 64 location-per-identity bounds report
`limit` when a candidate would exceed them. Unknown fields, duplicate keys or
identities, invalid numbers, excessive nesting and unsafe files are rejected.
An atomic replacement detected during a read triggers a bounded retry of the
current path. If the path keeps changing, the check returns `state_busy`.
Corrupt history is left untouched for explicit repair or removal.

## Invariants

1. A tracked id's verified location comes from the stable-id lookup. A saved-URL
   response for the same id must agree on the canonical location. Confirmed
   absence at the saved URL conflicts with a stable-id lookup that names it as
   the current location. Absence at an old URL can support a move elsewhere.
2. Existing locations remain an exact prefix. Bindings are immutable, and all
   existing identities and bindings are retained.
3. Under both locks, compare the entire document to the lookup's snapshot before
   reporting a result or writing a candidate. Any change yields `INCONCLUSIVE`
   with reason `conflict`, empty evidence and the other writer's state intact.
4. Classify a verified move using the requested URL, pre-check history and
   verified location. The response carries the pre-check history length.
5. The worker decoder enforces the producer's evidence shape for each reason and
   recomputes successful outcomes from the pre-check history length.

The store uses an in-process lock and the cross-process `ScheduleLock`. A changed
candidate is written through a temporary file, fsync and atomic replacement.

The inspector shows the current verified location and recorded previous locations.
An identity mismatch warns that the saved URL now refers to a different
repository. **Open verified new location in Browse** and **Save new location to
Library** are explicit actions. The latter uses normal Library saving. The old
entry remains accessible. New entries start with Auto-Archive off. Existing
entries retain their explicit settings. Existing path-based preservation jobs
are not automatically migrated or globally identity-bound. Rules, intervals,
scheduled configuration, release archives, manifests, Git mirrors, capture refs
and history bundles are never migrated, merged, rewritten or deleted. Checking
does not download assets or enqueue preservation. New-location storage remains
separate and is linked only through this history.

## Deferred enhancements

- Record the replacement occupant of a reused URL.
- Adopt a tracked alias after stable-id corroboration.
- Merge concurrent changes to independent records.

Native Windows handoff includes NTFS/reparse refusal, concurrent MSVCRT lock
contention and interruption around atomic replace. Exercise GUI cancellation,
deadline and shutdown in frozen onedir and onefile builds with
`--relocation-worker`.
The launcher statically imports the worker for PyInstaller discovery. Scheduled
Git mirror preservation remains deferred, separate from relocation checks.
