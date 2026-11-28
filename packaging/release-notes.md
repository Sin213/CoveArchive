# CoveArchive 1.2.0

## Repository Health Dashboard

Use **Check Selected** or **Check All** in Library's Health view to check public
GitHub repositories. The dashboard reports upstream availability, compares the
latest upstream releases with the archive and verifies local archive integrity.
These checks are read-only. **Update All** lets you review eligible updates using
saved rules. Downloads are queued only after an explicit confirmation.

## Scheduled release preservation

Enable **Scheduled preservation** in Settings to check opted-in GitHub
repositories while the window is closed. The OS scheduler runs short, anonymous
checks using saved Auto-Archive intervals, rules and archive destination. Before
downloading, the runner checks local archive integrity and stops for attention
if it finds damage or cannot complete the audit.
Adding to Library does not automatically enable Auto-Archive.

## Git history and repository relocation

- **Update Git Mirror** incrementally captures advertised branches and tags,
  preserving earlier refs even after upstream rewrites or deletions. **Verify
  Mirror** works offline, and captures can be exported and restored offline.
  **Archive Git History** still creates standalone bundle snapshots.
- When a complete Browse scan finds zero published releases, the **Source-only
  repository** view offers manual bundle and mirror actions for the scanned
  repository. A redirected GitHub URL must be rescanned at its canonical location.
- **Check repository location** in Library uses GitHub's stable repository ID to
  detect renames, owner transfers and reused URLs. Verified locations can be
  opened in Browse or saved as separate Library entries. Existing archive paths
  and preservation settings stay in place.

## Existing preservation capabilities

- Browse GitHub, Codeberg and GitLab.com releases, select assets by format and
  filename filters, and archive selected release assets through a persistent queue.
- Record metadata and verify downloads with SHA-256; verify existing completed
  assets before reuse and keep previously archived versions even when upstream
  listings change.
- Discover searches public GitHub only for emulators, recompilations, native
  ports and decomps. Open results in Browse or save them to Library collections.
  Classification is not proof of authenticity or playability. Check release
  availability and OS compatibility separately.

## Artifacts and supported platforms

- `CoveArchive-1.2.0-Setup.exe` — per-user Windows x86_64 installer.
- `CoveArchive-1.2.0-Portable.exe` — Windows x86_64 single executable.
- `CoveArchive-1.2.0-x86_64.AppImage` — Linux x86_64 AppImage.
- `covearchive_1.2.0_amd64.deb` — Linux amd64 Debian package.
- Each artifact has a matching `<artifact>.sha256` file; verify it with
  `sha256sum -c <artifact>.sha256` beside the downloaded artifact. Checksums are
  not a code-signing signature.

Windows requires 64-bit Windows 10 or newer, a desktop session and a local NTFS archive destination.
Linux requires glibc >= 2.35, a Wayland or X11 desktop and system desktop libraries
(including D-Bus, OpenGL, fontconfig, X11/xcb and Wayland); the Debian package
declares these dependencies. Linux archives need a local filesystem supporting
hard links and `/proc/self/fd`. No macOS or ARM builds are provided.
Git 2.45 or newer on PATH is required for Git preservation. Git and aria2 are
not bundled. Normal downloads use the built-in requests path.

## Data and limitations

Health, relocation checks and automatic release preservation apply to public
GitHub repositories. Scheduling is off by default. Installed Windows builds
with a stable onedir executable can register a task for the logged-in user.
Linux registration requires a working systemd user manager and a stable
installation path. The portable onefile executable and the AppImage do not support registration.

Git mirror updates are manual. Scheduled Git mirror preservation, byte-level
interrupted-download resume, version pinning and remembered asset selections are
deferred. Self-hosted providers and application auto-update are unsupported.

CA-N3b ships with documented review exceptions. Reused URLs are reported without
recording the replacement occupant, and new aliases of tracked IDs remain
inconclusive. Concurrent checks can require a retry. Known Windows sharing and
window-close edge cases are detailed in the [N3b roadmap section](https://github.com/Sin213/CoveArchive/blob/main/docs/ROADMAP.md#n3b-repository-relocation-history-complete-with-documented-exceptions).

All binaries are unsigned. Windows SmartScreen or antivirus may warn about them.
The portable executable uses the same `%LOCALAPPDATA%\CoveArchive` preferences,
Library and SQLite queue as the installer. User data and archives remain after
uninstalling the application.

Linux preferences and Library use `$XDG_CONFIG_HOME/covearchive` (default
`~/.config/covearchive`). The queue uses `$XDG_STATE_HOME/covearchive` (default
`~/.local/state/covearchive`). Relative XDG settings use the defaults.
Windows saves optional GitHub credentials in Windows Credential Manager.
Linux persistent credentials require available, unlocked KWallet 5 or 6.
Without it, newly pasted tokens remain session-only. There is no Secret Service
integration. Credentials are not saved in preferences, queue or archive metadata.

Only public, anonymously accessible assets on GitHub, Codeberg and GitLab.com
are supported. Optional GitHub tokens improve API rate limits for public
repositories. Git preservation omits LFS file contents, submodule contents and
unadvertised refs, including pull and merge request refs outside branches and tags.
Downloads are never executed or extracted. Hash verification is not a guarantee
that downloaded software is safe, and broad Linux distribution coverage is not
claimed. CoveArchive never automatically deletes archived files, historical
releases, Git snapshots or preserved Git refs. Upstream changes, Library removal
and disabling preservation leave that history in place.

AppImage includes xcb/xkb and D-Bus support libraries. Core host libraries such as
libc, libGL, libEGL and libstdc++ remain system requirements; they are deliberately
left to the host by PyInstaller's exclusion policy.
