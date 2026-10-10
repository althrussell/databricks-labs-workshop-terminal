# Take-home source export

`workshop-export` replaces the `promote` document pack. An attendee can say
“take my code home” or use the default pack's export card. The agent exports all
projects by default; a request about one project uses `--project`.

```bash
workshop-export
workshop-export --project "$HOME/projects/my-app"
```

Each export creates a distinct ZIP under
`/Volumes/$WORKSHOP_CATALOG/$WORKSHOP_SCHEMA/workshop_exports/code/`. These two
namespace variables come from CT's existing attendee provisioning. Both are
required: the helper never guesses `main`, creates another catalog/schema or
changes another principal's access. It uses WT's default coding identity;
attendee OBO scopes support SQL/metadata and do not support the Volume API.
CT already grants the coding identity `MANAGE` on the assigned catalog. The
helper uses that grant to add only its missing `USE_SCHEMA`/`CREATE_VOLUME` on
the assigned schema and `READ_VOLUME`/`WRITE_VOLUME` on `workshop_exports`.
Existing privileges are preserved. Missing CT build grants cause a clear error,
with the local ZIP retained. No CT code or OAuth scope changes are required.

Current source, including uncommitted/untracked files, tests, assets, dependency
manifests/lockfiles and existing project notes goes into the ZIP. Git history,
installed dependencies, builds/caches, managed harness skills, symlinks and common
environment/credential files are excluded. Example environment templates remain.
`EXPORT_INFO.txt` records omitted paths. Live Databricks resources and browser
storage are separate from the source export. Known hard-coded credentials must
be moved out of source before exporting.

Upload uses the supported `databricks fs` Volume commands. Downloaded bytes are
compared with the local ZIP before success is reported. Failed uploads or
verification retain the local ZIP in the attendee's private cache; a repeat
export preserves earlier archives.

The helper and agent show the exact Volume link and filename, followed by:

1. Open **Catalog → your catalog → your schema** in Databricks.
2. Open **Volumes → workshop_exports → code**.
3. Open the named ZIP's **File options → Download file**, then unzip it on your computer.

Download before the event ends: the temporary Volume disappears when the
workshop environment is deleted. The downloaded ZIP is the attendee's own copy.

Bootstrap/refresh removes the retired `promote` skill from warm shared trees.
Managed home links and existing managed project copies reconcile to the new
skill; attendee-owned instructions and assets remain preserved. Historical
document harvesting remains compatible so earlier sessions' records are intact.

This is the export portion of R08, in a separate PR based on R05's preparation
and skill-delivery changes. The wider project-continuity work remains separate.

Validated on the reused isolated Labs WT with a checksum-verified helper/skill
fixture, not a new full runtime deployment. A simple attendee request exported
93 source files across Repair Desk, Pollinator Pocket and a setup project.
Recovered garden source was identified as the deployed snapshot, and both apps'
exported source matched their exact deployed source. Upload read-back passed;
an actual Catalog Explorer download matched the verified Volume ZIP byte for
byte. Console download used the operator browser session; the attendee's
inherited `ALL_PRIVILEGES`/`MANAGE` on the Volume was verified independently.

The initial test exposed a missing assigned schema in WT's CT-simulation
runner and unsuitable OBO scopes for the Volume API. The WT-owned runner now
provisions the assigned schema and CT's attendee schema grant. Export uses the
default coding identity's existing CT `MANAGE` grant; a live retry after removing
the probe's self-grants proved automatic provisioning of only its required
schema access. No CT calls, CT code changes, OAuth scope changes or operator
grant mutations were needed. Local delivery/export/preparation/CT-runner suites
passed 189 tests, and release inventory/artifact checks passed 12 tests. Raw
screenshots, ZIPs and receipts remain outside Git.
