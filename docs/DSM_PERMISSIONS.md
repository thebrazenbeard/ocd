# DSM shared-folder permissions

OCD runs as the DSM 7 internal package service account `sc-OCD` (package ID `OCD`), not root.

Selecting or entering a path inside OCD does not magically grant filesystem rights.

## Root access

OCD installation does not select or grant access to a media share. The package starts with zero roots.

For every DSM shared folder you want OCD to manage:

1. DSM Control Panel → Shared Folder.
2. Edit the target share.
3. Permissions.
4. Change the account selector to **System internal user**.
5. Find **sc-OCD** (DSM may display it as the OCD system-internal package account).
6. Grant **Read/Write** if the root will use apply mode, or at minimum read/traverse for observe-only use.
7. Add the root through OCD's root manager/API/CLI.

Repeat this for as many TV, movie, and music roots as you need. OCD independently probes permissions when each root is registered. Apply mode is rejected when a create/remove probe fails.

## Why not root

DSM 7 explicitly moved third-party packages toward package-user execution and resource workers for privileged/system integration. OCD has no reason to run its entire daemon as root merely to rename media files.
