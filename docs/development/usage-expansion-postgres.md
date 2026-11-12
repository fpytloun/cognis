# Usage expansion PostgreSQL tests

`tests/integration/test_usage_expansion_postgres.py` exercises the public usage
query service against real PostgreSQL sessions and persistence. The separate
`test_usage_expansion_pg_contract.py` checks URL refusal, metadata, SQL compilation
and public constructors without connecting to PostgreSQL. A no-service pass is
not runtime PostgreSQL qualification.

## Isolated database contract

Runtime cases require both `COGNIS_TEST_USAGE_PG_ISOLATED=1` and
`COGNIS_TEST_POSTGRES_URL`. Missing settings skip the runtime cases. The marker is
an operator assertion, not proof of kernel isolation.

The runtime must own a new isolated network namespace and a disposable PostgreSQL
cluster. Do not use production, ambient services or a host-published port. The
accepted TCP URL is:

```text
postgresql+asyncpg://usage_test@127.0.0.1:5432/usage_expansion
```

Here, loopback belongs to the private namespace, not the host. IPv6 loopback is
also accepted. The alternative Unix socket is exactly `/run/cognis-usage-pg`,
with no hostname. The tests reject other roles or databases, passwords,
nonstandard ports, remote hosts and inherited connection options.

Use explicitly selected PostgreSQL and Python 3.12 test images with the project's
locked dependencies. Record their immutable identities and the exact source
revision. Keep source-first imports pointed at that source, not an image's old
checkout. Do not expose host credentials, environment files, caches, sockets or
executor configuration.

The namespace owner must bound runtime and resources, retain cancellation and
teardown ownership, and enforce cleanup even if pytest fails. Use a clean
environment, nonroot processes, private writable scratch and no external network
or host mounts. Hold the repository validation lock across setup, execution and
teardown; do not nest another Make lock inside the namespace.

## Fixture ownership and assertions

Each case creates a fresh `cognis_usage_expansion_<uuid>` schema. Its search path
contains only that schema, without a `public` fallback. The fixture creates the
actual dependency-closed core tables with `Base.metadata.create_all`, then calls
the canonical `ensure_usage_schema` for accounting tables, contracts and indexes.
Teardown disposes engines and drops only the fixture's own schema.

The cases cover lifetime and anchored execution history, managed descendants,
lineage and authority checks, currency/basis marginals, unknown and partial
money, measured zero, ranking refusals and membership budgets.

Snapshot tests use real AsyncSessions and an independent writer. They verify
repeatable-read/read-only behavior, SQLSTATE `25006` for a forbidden write,
snapshot visibility across writer commits, and pool reset. Timeout cases retain
the public query owner's normal statement budget.

Inside the isolated runner, execute the complete module serially:

```sh
python -m pytest tests/integration/test_usage_expansion_postgres.py -q -n 0 --maxfail=1
```

Collect the exact case inventory before qualification. Record actual outcomes,
skips, source/import provenance and teardown. Collection or skipped cases are
not runtime passes.

## No-service checks

From the checkout, leave runtime settings unset and use the normal shared lock:

```sh
env -u COGNIS_TEST_POSTGRES_URL -u COGNIS_TEST_USAGE_PG_ISOLATED \
  make test-serial PYTEST_ARGS='tests/integration/test_usage_expansion_pg_contract.py -q'
```
