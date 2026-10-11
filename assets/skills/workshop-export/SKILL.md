---
name: workshop-export
description: Export an attendee's workshop project source as a ZIP to their CT-assigned Unity Catalog volume, and show how to download it. Use for take-home, download-code, backup-source or end-of-workshop export requests.
---

# Take your workshop code home

Use `workshop-export` to package current source and verify its uploaded copy.
It exports all projects in `~/projects` by default, including unfinished work
and uncommitted files. For a request about one project, use its actual path:

```bash
workshop-export
workshop-export --project "$HOME/projects/<actual-project>"
```

Check that the projects the attendee names are present before exporting. If an
earlier session's source is in its recorded Workspace/deployment folder, recover
that existing source into their `~/projects` first. Skip installed dependencies,
managed skill directories and caches during recovery too. Keep the app itself unchanged;
do not rebuild missing code. If source cannot be found, name what is missing and
what the ZIP actually includes. List the included projects in the handoff.

The helper uses `WORKSHOP_CATALOG` and `WORKSHOP_SCHEMA` supplied by Control
Tower, and creates/reuses `workshop_exports` inside that existing namespace.
It uses WT's default coding identity, including when attendee OBO is enabled.
CT already grants that identity `MANAGE` on the attendee catalog. The helper
adds only missing `USE_SCHEMA`/`CREATE_VOLUME` permissions for itself in the
assigned schema and `READ_VOLUME`/`WRITE_VOLUME` on this export Volume. It does
not grant other principals access, create a catalog/schema or fall back to
`main`. If that existing CT access is missing, report the error and retained
local ZIP and ask the organizer to check the configuration.

Export source, tests, assets, dependency manifests/lockfiles and existing
README/PRODUCT/DESIGN notes. The helper excludes installed dependencies,
generated builds, caches, Git history, managed harness skills, symlinks and
environment/credential files; example environment templates are included.
If a credential is known to be hard-coded in source, replace it with an
environment variable before exporting. Do not copy home configuration or
tokens into the project to make it run elsewhere. Databricks resources and
application data are separate from this source export.

After a verified upload, show the attendee the exact Volume link, ZIP filename
and these download steps on screen:

1. Open **Catalog** in Databricks, then the assigned **catalog → schema**.
2. Open **Volumes → workshop_exports → code**.
3. Open the named ZIP's **File options → Download file**, then unzip it on their computer.

Say briefly: **Download before the workshop ends. The Volume is deleted with
the workshop environment; your downloaded ZIP is the copy you keep.**

Keep this handoff short. Use existing project notes; no architecture/security/
Jira document pack or rebuild is needed. Report unfinished work and material
limitations honestly. If upload/verification fails, retain the local ZIP and
report the failure; do not claim the attendee has downloaded or saved it.
