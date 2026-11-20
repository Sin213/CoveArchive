# CoveArchive 1.1.0

## Discover

Search public GitHub repositories in CoveArchive to find emulators,
recompilations, native ports and decomps. Filter by project category and sort by
relevance, stars or recent updates. Review classification evidence and project
information, then open discovered repositories in Browse to check their releases.

## Library integration

Save discovered repositories to Library, with duplicates detected automatically.
Organize them into collections, open GitHub project pages and copy repository
URLs. After saving, optionally configure the existing Auto-Archive settings.

## Existing preservation capabilities

- Browse GitHub, Codeberg and GitLab.com releases, select assets by format and
  filename filters, and archive selected release assets through a persistent queue.
- Record metadata and verify downloads with SHA-256; verify existing completed
  assets before reuse and keep previously archived versions even when upstream
  listings change.
- Preserve public Git history as optional Git bundles using an external Git install.

## Artifacts and supported platforms

- `CoveArchive-1.1.0-Setup.exe`: per-user Windows x86_64 installer.
- `CoveArchive-1.1.0-Portable.exe`: Windows x86_64 single executable.
- `CoveArchive-1.1.0-x86_64.AppImage`: Linux x86_64 AppImage.
- `covearchive_1.1.0_amd64.deb`: Linux amd64 Debian package.
- Each artifact has a matching `<artifact>.sha256` file; verify it with
  `sha256sum -c <artifact>.sha256` beside the downloaded artifact. Checksums are
  not a code-signing signature.

Windows requires 64-bit Windows 10 or newer, a desktop session and a local NTFS archive destination.
Linux requires glibc >= 2.35, a Wayland or X11 desktop and system desktop libraries
(including D-Bus, OpenGL, fontconfig, X11/xcb and Wayland); the Debian package
declares these dependencies. Linux archives need a local filesystem supporting
hard links and `/proc/self/fd`. No macOS or ARM builds are provided.
Git 2.45 or newer on PATH is optional, required only for Git-history bundles.
Git and aria2 are not bundled; normal downloads use the built-in requests path.

## Data and limitations

Discover searches public GitHub only. Classification is not proof of authenticity or playability.
GitHub metadata does not confirm official status, OS compatibility or release availability.
Adding to Library does not automatically enable Auto-Archive.
Auto-Archive operates only while the application is running.

All binaries are unsigned. Windows SmartScreen or antivirus may warn about them.
The portable executable uses the same `%LOCALAPPDATA%\CoveArchive` preferences,
Library and SQLite queue as the installer; it does not store them beside the exe.
Uninstalling removes the application installation, not user data or archives.

Linux preferences and Library use `$XDG_CONFIG_HOME/covearchive` (default
`~/.config/covearchive`); the queue uses `$XDG_STATE_HOME/covearchive` (default
`~/.local/state/covearchive`). Relative XDG settings use the defaults.
Windows saves optional GitHub credentials in Windows Credential Manager.
Linux persistent credentials require available, unlocked KWallet 5 or 6;
otherwise newly pasted tokens remain session-only. There is no Secret Service
integration. Credentials are not saved in preferences, queue or archive metadata.

Only public, anonymously accessible assets on the supported hosts are supported.
GitHub tokens improve rate limits, not private-repository access. There is no
background service, operation while closed, byte-level resume or auto-update.
Git bundles omit LFS file contents, submodule contents and PR/MR refs.
Downloads are never executed or extracted. Hash verification is not a guarantee
that downloaded software is safe, and broad Linux distribution coverage is not
claimed. Existing archives are never pruned automatically.

AppImage includes xcb/xkb and D-Bus support libraries. Core host libraries such as
libc, libGL, libEGL and libstdc++ remain system requirements; they are deliberately
left to the host by PyInstaller's exclusion policy.
