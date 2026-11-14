"""Archive naming, validated metadata, pure planning and local persistence.

CA-S5 uses pinned handles on an authorized local NTFS root: relative operations,
exclusive temps, no-replace assets, size/hash verification and locked manifests.
Reparse points, 8.3/non-case aliases and hard-linked verification targets are
rejected; only owner/repo project folders reuse unique ASCII case variants with
their established on-disk spelling; ambiguous siblings are rejected even for an
exact request, and non-ASCII names require exact spelling. A non-cooperating process can replace a manifest
between validation and publication, and CoveArchive may overwrite it without
detecting conflict; unlink checks before/after publication are best-effort, narrow
the window, are not an atomic compare-and-replace and do not detect every foreign
replacement; this is an accepted CA-S5.1 limitation, without guaranteed
compare-and-replace.
Paths above the root resolve once. File flushes provide no power-loss guarantee;
completion verifies bytes at a point in time, not against later external edits.
Only exact internal names are reserved within their project/release role.
"""
