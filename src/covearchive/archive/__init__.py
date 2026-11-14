"""Archive naming, validated metadata, pure planning and local persistence.

CA-S5 uses pinned handles on an authorized local NTFS root: relative operations,
exclusive temps, no-replace assets, size/hash verification and locked manifests.
Reparse points, case aliases and hard-linked verification targets are rejected.
Paths above the root resolve once. File flushes provide no power-loss guarantee;
completion verifies bytes at a point in time, not against later external edits.
Only exact internal names are reserved within their project/release role.
"""
