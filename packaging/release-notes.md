# CoveArchive 1.0.0

CoveArchive preserves public project release assets in a local archive:

- Browse GitHub, Codeberg and GitLab.com releases, select assets by format and
  filename filters, and download through a persistent queue.
- Record metadata and SHA-256 hashes; verify existing completed assets before
  reuse and preserve conflicts and earlier archives instead of overwriting them.
- Organize GitHub repositories in Library collections, import/export repository
  lists, and opt in to Auto-Archive while the application window is open.
- Manually archive Git history as bundles using an optional external Git install.

## Artifacts and supported platforms

- `CoveArchive-1.0.0-Setup.exe`: per-user Windows x86_64 installer.
- `CoveArchive-1.0.0-Portable.exe`: Windows x86_64 single executable.
- `CoveArchive-1.0.0-x86_64.AppImage`: Linux x86_64 AppImage.
- `covearchive_1.0.0_amd64.deb`: Linux amd64 Debian package.
- `SHA256SUMS.txt`: artifact checksums, not a code-signing signature.

Windows requires 64-bit Windows 10 or newer, a desktop session and a local NTFS archive destination.
Linux requires glibc >= 2.35, a Wayland or X11 desktop and system desktop libraries
(including D-Bus, OpenGL, fontconfig, X11/xcb and Wayland); the Debian package
declares these dependencies. Linux archives need a local filesystem supporting
hard links and `/proc/self/fd`. No macOS or ARM builds are provided.
Git 2.45 or newer on PATH is optional, required only for Git-history bundles.
Git and aria2 are not bundled; normal downloads use the built-in requests path.

## Data and limitations

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
