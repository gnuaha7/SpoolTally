# SpoolTally

SpoolTally is a local service for managing a 3D printer filament inventory. This release provides database initialization and migrations, token administration, an HTTP health endpoint, full-access location management, and development checks.

## Set up a local environment

Use Python 3.13. Create a virtual environment and install the package with its development tools:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## Initialize the data directory

Initialize the default data directory with:

```sh
.venv/bin/python -m spooltally init
```

The default directory is `~/.local/share/spooltally`. Set `SPOOLTALLY_DATA_DIR` or pass `--data-dir PATH` to choose another directory. The command-line option takes precedence over the environment variable. SpoolTally expands `~` and resolves the path to an absolute path. The SQLite database is named `spooltally.sqlite3` inside that directory. Initialization applies database migrations and reports `schema_status` as `current`.

The database directory must be on a local filesystem. SQLite on network filesystems, including NAS storage, is unsupported. SpoolTally does not detect or reject network filesystems.

## Manage access tokens

Create a token, list configured tokens, and revoke a token by its ID:

```sh
.venv/bin/python -m spooltally tokens create --name printer
.venv/bin/python -m spooltally tokens list
.venv/bin/python -m spooltally tokens revoke TOKEN_ID
```

To use a different data directory, put `--data-dir PATH` after the token command, for example `.venv/bin/python -m spooltally tokens list --data-dir ./data`. Token creation prints the raw token only once, in the successful creation response; copy it then. Listing and revoking never return the raw token. Revoked tokens remain in the list and can no longer authenticate. The only supported role is `full_access`.

## Start the service

Start the service after initialization:

```sh
.venv/bin/python -m spooltally serve
```

The service binds to `127.0.0.1` on port `8000` by default. Use `--host` and `--port`, or set `SPOOLTALLY_HOST` and `SPOOLTALLY_PORT`. Command-line options take precedence over environment variables. The host must be an IP address or `localhost`. The port must be a decimal number from `1` to `65535`.

`serve` does not run database migrations. It requires the initialized database file. Startup and runtime logs go to standard error. Check readiness at the root health endpoint:

```sh
curl http://127.0.0.1:8000/healthz
```

When the database opens and answers a query, the response is HTTP `200` with:

```json
{"status":"ready"}
```

The health endpoint does not require authentication or an application schema. It reports process and database connectivity only. It does not return inventory data, token data, filesystem paths, or schema details.

## Manage locations

Location endpoints use `/api/v1/locations` and require a `full_access` bearer token. Replace the placeholders below with a token created by the CLI and a unique idempotency key:

```sh
curl -i http://127.0.0.1:8000/api/v1/locations \
  -H 'Authorization: Bearer <TOKEN>'

curl -i -X POST http://127.0.0.1:8000/api/v1/locations \
  -H 'Authorization: Bearer <TOKEN>' \
  -H 'Idempotency-Key: <UNIQUE_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"Shelf A"}'
```

Create returns the location and an opaque quoted `ETag`. Copy that header value exactly for updates and deletion. `PATCH` and `DELETE` require it as `If-Match`; a missing precondition returns `428`, and a stale tag returns `412`. Repeating a successful write with the same idempotency key and request returns its original result; reusing the key for a different request returns `409`.

```sh
curl -i -X PATCH http://127.0.0.1:8000/api/v1/locations/<LOCATION_ID> \
  -H 'Authorization: Bearer <TOKEN>' \
  -H 'If-Match: <COPY_QUOTED_ETAG>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"Shelf B"}'

curl -i -X DELETE http://127.0.0.1:8000/api/v1/locations/<LOCATION_ID> \
  -H 'Authorization: Bearer <TOKEN>' \
  -H 'If-Match: <COPY_QUOTED_ETAG>'
```

## Run project checks

Run the test suite, linter, and type checker from the repository root:

```sh
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
```

## Current scope

CORE-03 provides the SQLite schema and migrations. CORE-04 provides offline token creation, listing, revocation, and full-access authentication. CORE-05 provides location CRUD with idempotent writes and ETag preconditions. Filament and spool inventory operations, metrics access, and metrics-only tokens are not included.

See [AGENTS.md](AGENTS.md) for contribution rules.
