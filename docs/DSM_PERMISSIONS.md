# DSM shared-folder permissions

OCD runs as the DSM 7 internal package service account `sc-OCD` (package ID `OCD`), not root.

Selecting or entering a path inside OCD does not magically grant filesystem rights.

## Initial root

The installation wizard asks for one DSM shared-folder name and media type. The SPK's `conf/resource` uses DSM's `data-share` worker to grant `sc-OCD` read/write access. `postinst` stages the wizard values in private package state without requiring the share to exist yet. On first service start, after DSM has acquired the resource, OCD resolves `/var/packages/OCD/shares/<share>`, validates access, stores the real path, and deletes the bootstrap file only after success.

## Additional roots

For additional existing shared folders:

1. DSM Control Panel → Shared Folder.
2. Edit the target share.
3. Permissions.
4. Change the account selector to **System internal user**.
5. Find **sc-OCD** (DSM may display it as the OCD system-internal package account).
6. Grant **Read/Write** if the root will use apply mode, or at minimum read/traverse for observe-only use.
7. Add the root through OCD.

OCD independently probes permissions when a root is registered. Apply mode is rejected when a create/remove probe fails.

## Why not root

DSM 7 explicitly moved third-party packages toward package-user execution and resource workers for privileged/system integration. OCD has no reason to run its entire daemon as root merely to rename media files.
