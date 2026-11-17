# CoveArchive

**v1.0.0 (upcoming)** - local preservation of public project releases.

CoveArchive is a desktop application for keeping release assets in a local
archive you control. Releases can disappear, assets can be replaced, and an
upstream project can change its distribution plans. CoveArchive saves selected
files, records their metadata and SHA-256 hashes, and keeps previously archived
files when the upstream listing changes.

CoveArchive is part of the Cove Nexus family of tools. It is a standalone
application; Cove Nexus is not required.

This upcoming version runs from source on Windows and Linux. Installers and
release binaries are not available yet.

## Supported sources

CoveArchive supports public repositories on these hosts:

| Source | Repository URL | Release-list URL | Eligible release assets |
| --- | --- | --- | --- |
| GitHub | `https://github.com/owner/repository` | `https://github.com/owner/repository/releases` | Uploaded release assets |
| Codeberg | `https://codeberg.org/owner/repository` | `https://codeberg.org/owner/repository/releases` | Native Codeberg attachments with known sizes |
| GitLab.com | `https://gitlab.com/group/project` | `https://gitlab.com/group/project/-/releases` | Release links to the same project's public generic-package files, validated against package metadata |

GitLab subgroup paths are supported, such as `group/subgroup/project`.
URLs may omit `https://`; GitHub also accepts `www.github.com`.
Use a repository or release-list URL in Browse. Individual release-tag URLs,
other hosts and self-hosted forges are unsupported.

All supported projects and assets must be anonymously accessible. An optional
GitHub token can help with API limits; it does not enable private repositories.
Codeberg and GitLab use anonymous access only. Arbitrary external release links
are not downloadable assets in this workflow.

| Feature | GitHub | Codeberg | GitLab.com |
| --- | --- | --- | --- |
| Browse, filters, release archiving and Queue | Yes | Yes | Yes |
| Manual Archive Git History | Yes | Yes | Yes |
| Save Rules, active Library entries and Auto-Archive | Yes | No | No |

Imported non-GitHub Library entries can remain in the list as inactive metadata.
Browse support for Codeberg and GitLab does not make those Library entries active.

## Requirements and source launch

- Python 3.12 or newer.
- `requests` and PySide6, installed by the commands below.
- A Windows desktop session, or a Linux desktop session with Wayland or X11.
- An existing, absolute local archive directory with enough free space.
- Git 2.45 or newer on `PATH` only if you want Git-history bundles.

On Windows, the archive destination must be on a local NTFS volume.
On Linux, use a local filesystem that supports hard links and provides access
to `/proc/self/fd`; unsupported archive publication fails without replacing files.
Linux-specific filesystem tests are included; broad distribution coverage is not claimed.

From the repository root, create a virtual environment and install the project.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe run_covearchive.py
```

### Linux

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python run_covearchive.py
```

With the intended Python environment active, the equivalent launch command is
`python run_covearchive.py`.

## Browse and archive releases

1. Paste a supported repository or release-list URL into **Browse** and choose
   **Scan**. Expand releases to inspect their assets.
2. Choose formats, whether to include prereleases, and any Smart Build Filters.
3. Check individual eligible assets or use **Select Matching**. Review the
   selection before archiving.
4. Choose an existing local archive destination and select **Archive Selected**.
5. Open **Queue** to monitor downloads and use **Start Queue** when work is waiting.

Only checked assets that still match the current eligibility rules are queued.
Scanning and changing filters do not remove archived files. For GitHub projects,
use **Save rules for this repository** to retain the format, prerelease and
filename rules for later use, including Auto-Archive.

### Supported formats

Suffix matching is case-insensitive for ASCII letters.

| Format | Filename suffix | Default selection |
| --- | --- | --- |
| AppImage | `.appimage` | On |
| TAR.GZ | `.tar.gz` | On |
| TAR | `.tar` | On |
| DMG | `.dmg` | On |
| ZIP | `.zip` | On |
| 7z | `.7z` | On |
| EXE | `.exe` | Off; opt in |
| MSI | `.msi` | Off; opt in |

Files are archived only. CoveArchive never launches, installs or extracts them,
including EXE and MSI files.

### Smart Build Filters

Include and exclude fields accept comma-separated shell-style filename patterns,
such as `*linux*, *x86_64*`. Patterns match filenames, not paths, and ignore ASCII
letter case.

- Any include pattern may match; an empty include field allows all filenames.
- Any exclude match wins over an include match.
- Use `[,]` to match a literal comma.

Review the match preview: `*win*` also matches `darwin`. These are filename rules,
so they cannot reliably infer an operating system or architecture.

## Queue

The persistent queue records jobs and their state between application sessions.
Opening CoveArchive does not start restored jobs. Use **Start Queue** to run
earlier pending work.

Newly enqueued work can start automatically if there is no earlier pending work,
or can join an already running queue. If earlier jobs are waiting while the queue
is idle, the new work also waits for **Start Queue**. This applies to Browse and
Auto-Archive submissions. Resume and Retry use the same queue-start rules.

- **Pause** stops a waiting job or requests that a running transfer stop.
- **Resume** makes a paused job eligible to run again.
- **Retry** requeues failed or cancelled jobs.
- **Cancel** stops the selected job without deleting completed archived files.

There is no byte-level resume: an incomplete download restarts from the beginning.
Rate-limit cooldowns and retry delays may keep eligible jobs waiting.
When closing during downloads, **Pause and Close** waits for work to stop.

A completed queue row is a historical result, not continuous monitoring of the
file. Requeuing a completed asset checks the archived bytes again.

## Library and Auto-Archive

### Organize repositories

Use **Library** to keep GitHub repositories in collections, search the list,
rename entries and move them between collections. **Bulk Add** accepts one
GitHub repository URL per line and skips duplicates.

Import and export use UTF-8 JSON:

| Operation | Accepted or produced format |
| --- | --- |
| Export | CoveArchive repository-list JSON: `format: "covearchive-repository-list"`, `version: 1` |
| Import | The same CoveArchive format, or Git-Sync JSON: `format: "Git-Sync data"`, `version: 1`, with a `repos` list |

Imports show a preview before applying changes. Duplicates are skipped; invalid
imports are rejected. Collections and descriptive repository metadata are
portable; exports do not contain archived files, tokens, saved build rules or
Auto-Archive enablement. Imported retention settings do not authorize deletion.

Removing Library entries or deleting collections does not delete archives.

### Opt in to automatic release checks

Auto-Archive is **off by default**, applies only to active GitHub Library entries,
and runs only while the application window is open.

To enable it:

1. Scan the GitHub repository in Browse, choose formats and filters, and save rules.
2. Choose an archive destination and archive an asset once to save that destination.
3. Select the repository in Library, choose **Auto-Archive...**, and select an
   interval: **1, 6, 12 or 24 hours**.

The first check waits for the selected interval. Reopening the application
starts a fresh interval; there is no closed-window catch-up service.
Checks use the saved rules and current saved archive destination to enqueue
eligible release assets through the normal queue. Completed jobs are not
automatically requeued.

Auto-Archive only scans and enqueues; the normal queue performs downloads and
verification and may start under the rules described above. It does not create
Git-history bundles. Turning it off stops future checks and leaves queued jobs
and archived files in place. There is no background service, tray agent or
scheduler daemon.

## Preservation and archive layout

Completed release files are checked for expected size and SHA-256. When the
provider supplies a publisher SHA-256 digest, that digest is checked too.
A local hash records the bytes saved; it does not establish publisher authenticity
when no publisher digest is available.

Existing completed files are verified before being reused by an archive job.
Conflicting files are preserved rather than overwritten. Upstream changes may
require attention instead of silently replacing an earlier download.

Project directories are separated by provider:

| Source | Project directory |
| --- | --- |
| GitHub | `<archive root>/Owner/Repo/` |
| Codeberg | `<archive root>/codeberg.org/Owner/Repo/` |
| GitLab.com | `<archive root>/gitlab.com/Group/Subgroup/Project/` |

The subgroup portion is present only where applicable. Directory and asset names
are made filesystem-safe when necessary; original asset names remain in metadata.
A project contains:

```text
<project directory>/
  archive-index.json
  release-<safe-tag>--<stable-identifier>/
    release.json
    <saved asset filename>
  git-history/
    snapshot-<UTC timestamp>--<reference digest>/
      history.bundle
      history.json
```

`archive-index.json` records project identity, release folders and saved names.
`release.json` records release and asset metadata, including sizes, hashes and
completion state. Assets sit beside `release.json`. The optional `git-history`
directory appears when you archive Git history.

**Non-deletion guarantee:** CoveArchive never deletes archived release files or
Git-history snapshots. Upstream removal of releases or assets, changes to filters
or settings, Library removal and disabling Auto-Archive do not delete them.
Snapshots are never pruned automatically; deleting archived files or snapshots
is your choice. Internal temporary files may be cleaned up.

Metadata and queue formats are versioned. Builds that encounter newer schemas
they do not understand refuse to use them; do not assume an older build can
reopen data written by a newer one.

## Optional: archive Git history

After scanning a supported repository and choosing a destination, select
**Archive Git History** explicitly. This manual action requires Git 2.45 or newer
and saves a self-contained bundle of the history reachable from the repository's
advertised branches and tags, with a `history.json` manifest and bundle SHA-256.

Bundles exclude pull-request and merge-request refs, Git LFS file contents and
submodule repository contents. LFS pointers and submodule references may be part
of the Git history, but their external contents are not backed up.
No working tree is checked out by CoveArchive.

Changed branch or tag refs produce another snapshot. Unchanged refs can reuse a
previous snapshot after its bundle hash is verified. Existing snapshots remain,
including when branches or tags disappear upstream.

To restore a bundle as a local mirror, use Git with paths of your choice:

```sh
git clone --mirror --ref-format=reftable "<absolute snapshot directory>/history.bundle" "<restored repository>"
git -C "<restored repository>" bundle verify "<absolute snapshot directory>/history.bundle"
git -C "<restored repository>" fsck --full
```

`--ref-format=reftable` selects the reftable ref format (Git 2.45 or newer) to
restore branch/tag names that would collide on Windows filesystems, such as
names differing only by case or reserved device names like `aux`.

These checks validate Git structure; compare the bundle's SHA-256 with
`history.json` separately if you need to check the saved file against its manifest.

## Optional GitHub token

Anonymous access works without a token. Add or clear an optional GitHub token
in **Settings** to use authenticated API rate limits for public projects.

- Windows stores saved tokens in **Windows Credential Manager**.
- Linux uses **KWallet 6 or 5** when available and unlocked; otherwise a newly
  pasted token is held only in memory for the current session.
- If loading a saved token from KWallet fails, that saved token is not used in
  the session. Unlock KWallet and restart CoveArchive to load it.
- If saving fails, the pasted token remains usable for that session. Fix or
  unlock the vault and paste it again to save it persistently.

Tokens are sent only to `api.github.com`, never to release storage hosts,
Codeberg, GitLab or Git-history commands. Tokens are never stored in preferences,
the queue or archive manifests. If clearing the vault fails, CoveArchive stops
using the token in memory and reports that the saved credential still needs removal.

CoveArchive uses the token only for repositories GitHub reports public via one authenticated visibility check; otherwise release and asset requests stay anonymous.

## Settings and local data

| Platform | Preferences and Library | Queue |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\CoveArchive\preferences.json` and `library.json` | `%LOCALAPPDATA%\CoveArchive\queue.sqlite3` |
| Linux | `$XDG_CONFIG_HOME/covearchive/preferences.json` and `library.json`; default `<user home>/.config/covearchive/` | `$XDG_STATE_HOME/covearchive/queue.sqlite3`; default `<user home>/.local/state/covearchive/` |

Relative XDG directory values are ignored in favor of the defaults.
Keep local data and archives together in your own backup plan if you want to
preserve both the saved files and application organization.

## Security, privacy and limitations

Release discovery and downloads use HTTPS with TLS certificate verification.
Download redirects are checked against provider-specific host allowlists;
unknown destinations are rejected. Credentials are not stored in archives.
CoveArchive has no telemetry.

Archived assets are published without replacing existing files. Windows and Linux
archive handling rejects unsafe paths and symbolic-link redirection beneath the
chosen archive root; Windows also rejects junctions and reparse points.
Downloads are never executed or extracted.

Verification describes the bytes at the time they were checked. Other processes
running as the same user, or with administrator privileges, can modify closed
files. CoveArchive does not make files immutable or promise power-loss durability.
Preservation is not a guarantee that downloaded software is safe to run.

Archive metadata files (`archive-index.json` and `release.json`) are written
carefully, but another process that replaces them at the same moment may overwrite
or be overwritten by CoveArchive without detection. CoveArchive does not guarantee
atomic replacement or protection against concurrent external modification.

- Public repositories and anonymously accessible assets only; no private
  repositories, authenticated Codeberg/GitLab, self-hosted forges or other hosts.
- Repository and release-list inputs only; no individual tag URLs in the UI.
- Provider rate limits, network failures and bounded scans can delay or prevent
  discovery or downloads; very large scans may time out.
- No background operation while the window is closed and no byte-level resume.
- Git bundles omit LFS file contents, submodule contents and PR/MR refs.
- Windows and Linux source use is covered here; macOS support is not claimed.

## Screenshots

No approved, sanitized screenshots are available yet. Screenshots will be added
once approved captures are available.

## Development and testing

From the repository root, with the intended Python environment active:

```sh
python -m pip install -e ".[test]"
python -m pytest
```

The tests use offline fixtures and local resources. Platform-specific filesystem
tests run only on their target platform, and Git-history tests require Git.
The `--smoke-test` launcher option is a developer diagnostic that briefly opens
and closes the application.

## License

MIT. See [LICENSE](LICENSE).
