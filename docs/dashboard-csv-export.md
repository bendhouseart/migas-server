# Dashboard telemetry TSV export

The dashboard can export project telemetry as a flat TSV for offline analysis.
Each TSV row represents one breadcrumb and contains every telemetry field that
can be joined to it from PostgreSQL:

```text
crumbs.user_id -> users.user_id
users.geoloc_idx -> geoloc.idx
```

Both joins are left joins. A crumb is therefore exported even when it is
anonymous, its user record is missing, or no geolocation was recorded.

The `projects` table contributes only the same project name already stored on
the crumb, so it is not duplicated. Authentication records are deliberately
excluded: tokens and token metadata are authorization data, not telemetry, and
must never be included in an offline export.

## Dashboard modes

The dashboard exposes three export actions:

- **Export TSV** exports the visible chart range.
- **Export All** exports the selected project's complete database history.
- **Export Custom** exports a user-supplied date range.

An active version filter applies to all three modes. Export All sends no date
bounds; it no longer derives an incomplete range from the browser's dashboard
cache.

All modes use one browser download helper. The clicked button owns its busy
state, error handling, response filename, and cleanup.

## API

All dashboard modes call the same authenticated endpoint:

```http
GET /api/usage-export/{project}
Authorization: Bearer {dashboard-token}
```

The query parameters are optional:

| Parameter | Type | Meaning |
| --- | --- | --- |
| `start` | ISO-8601 datetime | Include crumbs at or after this instant |
| `end` | ISO-8601 datetime | Include crumbs at or before this instant |
| `version` | string | Include only this project version |

Omitting both dates exports all history. Supplying only one date creates an
open-ended range. If both are supplied and `start > end`, the endpoint returns
HTTP 400.

The endpoint uses the existing project-scoped authorization dependency.
Project tokens can only export their project; master authorization retains its
existing cross-project behavior. Unknown projects return HTTP 404.

## TSV schema

Crumb column names are retained for compatibility. Joined identifiers are
named explicitly where their source would otherwise be ambiguous.

| Source | TSV columns |
| --- | --- |
| `crumbs` | `idx`, `project`, `version`, `language`, `language_version`, `timestamp`, `session_id`, `user_id`, `status`, `status_desc`, `error_type`, `error_desc`, `is_ci`, `params` |
| `users` | `user_idx`, `joined_user_id`, `user_type`, `platform`, `container`, `geoloc_idx` |
| `geoloc` | `joined_geoloc_idx`, `asn`, `asn_org`, `continent_code`, `country_code`, `state_province_name`, `city_name`, `lat`, `lon` |

`user_id` is the foreign-key value stored on the crumb.
`joined_user_id` is the value found in the joined user row. Likewise,
`geoloc_idx` comes from the user and `joined_geoloc_idx` confirms the
geolocation row that was found. Keeping both values makes incomplete or
inconsistent historical data visible during offline analysis.

The fixed column order is explicit and stable. Adding a telemetry field to one
of these tables requires adding it to `TELEMETRY_EXPORT_COLUMNS` and the
matching select expression in `migas/server/export.py`. Flattened parameter
columns follow the fixed columns in sorted path order.

## Structured telemetry

The `params` JSONB value is always retained as compact JSON in the `params`
cell. Its leaf values are also expanded into dynamic TSV columns for offline
analysis:

- Every derived column is namespaced with `params.` so its source is explicit.
- A top-level scalar such as `iam` becomes the `params.iam` column.
- Nested objects use dotted paths after the namespace, such as
  `params.input.modality`.
- Arrays remain compact JSON in one cell rather than becoming multiple rows or
  indexed columns.
- Empty and null values remain represented in the original `params` JSONB.
- A parameter named `project` becomes `params.project`; it cannot be confused
  with the fixed crumb `project` column.

For example, a database value such as:

```json
{"iam":"newparam","input":{"modality":"T1w"},"flags":["offline","batch"]}
```

produces `params.iam`, `params.input.modality`, and `params.flags` columns while
preserving the complete object in `params`. It remains parseable with a TSV
reader followed by a JSON parser where appropriate:

```python
import csv
import json

with open("migas-project-all.tsv", newline="") as stream:
    for row in csv.DictReader(stream, delimiter="\t"):
        params = json.loads(row["params"]) if row["params"] else None
        flags = json.loads(row["params.flags"]) if row["params.flags"] else None
        modality = row["params.input.modality"]
```

Datetimes are serialized as ISO-8601 strings, enum values use their database
value, and SQL nulls become empty TSV cells.

## Streaming behavior

The server first streams only the matching `params` values to discover the
union of flattened column paths. It does not retain the telemetry rows. It then
uses a PostgreSQL server-side result stream and writes one TSV row at a time:

```text
parameter-key prepass -> PostgreSQL joined-row cursor -> TSV row -> HTTP StreamingResponse
```

This replaces the earlier path that loaded all database rows and the complete
export into server memory. Dynamic columns require a bounded-memory first pass,
so the matching `params` values are scanned twice. Rows are ordered by crumb
timestamp and then crumb index, giving deterministic output.

The browser currently uses `Response.blob()` because authenticated downloads
need the Bearer token. Consequently, the server is streaming-safe but the
browser still holds the completed file in memory before saving it. If exports
grow beyond practical browser memory, the next step is an asynchronous export
job with expiring object-storage downloads rather than another synchronous
endpoint.

## Response

A successful response includes:

```http
Content-Type: text/tab-separated-values; charset=utf-8
Content-Disposition: attachment; filename="migas-{project}-{range}.tsv"
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

Unsafe filename characters, including project-name slashes, are replaced with
hyphens. An all-history export uses the suffix `-all.tsv`. Empty projects
return a header-only TSV containing the fixed telemetry columns.

## Data fidelity and spreadsheet safety

Exports preserve raw telemetry values. Text beginning with `=`, `+`, `-`,
or `@` is not rewritten, because escaping it would change the offline dataset.
Consumers opening untrusted exports directly in spreadsheet applications
should use an import mode that does not evaluate formulas. A future
spreadsheet-safe presentation export should be a separate format from this
lossless telemetry export.

## Test coverage

The export tests cover:

- timestamp and version filtering;
- project-scoped authorization;
- all-history export with no date bounds;
- all crumb, user, and geolocation fields;
- recursively flattened JSON objects and array-valued TSV cells;
- anonymous crumbs and missing join rows;
- empty projects;
- reversed date ranges;
- deterministic, unique column names; and
- download security headers.

The dashboard JavaScript is syntax-checked separately. Browser automation is
not currently part of this repository's test suite.
