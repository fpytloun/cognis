# Program runtime policy ownership

The step runtime owns a read-only resolver and an immutable mapping of spawned
in-process connection IDs to configured executor IDs. Each decision reads the
current owner-scoped executor row; the database session closes before executor
I/O. The agent loop resolves the actual target and checks before program RPC
preparation; the tool router checks again before dispatch. Existing assignment
and Intaris authorization remain separate.

The resolver persists no state and takes no locks or transactions across I/O.
The turn scheduler owns cancellation and existing cleanup remains unchanged.
Revocation applies to new decisions, not already-dispatched programs. Retry
uses the same current-row check and does not retry an accepted call. Ordinary
tool and executor transport contracts are unchanged.
