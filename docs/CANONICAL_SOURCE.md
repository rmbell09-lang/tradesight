# TradeSight canonical source

The only operational TradeSight source tree is:

`/Users/luckyai/Projects/TradeSight`

The dashboard, paper trader, optimizer, tests, packaging, and future releases must
all be run from this tree. Other TradeSight directories are historical copies and
must not be used for development, services, packaging, or recovery without an
explicit reconciliation against the canonical tree.

The machine-readable contract is `ops/canonical_source.json`. Verify it with:

```bash
python3 scripts/verify_canonical_source.py
```

The verifier fails closed when the path, branch, clean remote URL, launch-agent
paths, paper-only declaration, or recovery snapshot is wrong.

## Recovery checkpoint

The pre-package snapshot is stored on the FileVault-protected internal drive at:

`/Users/luckyai/Backups/TradeSight/pre-packages-1-2-20260714-1142ET`

It contains a Git bundle, tracked worktree patch, complete non-Git working-tree
archive, status inventories, and SHA-256 checksums. The snapshot was verified
before package work began.

## Credential status

The Git remote is credential-free. The previously embedded credential was also
removed from local reflog messages. The old token must be treated as compromised
and rotated through GitHub before a future push. The token itself is not stored in
this document or the recovery bundle.

## Operational rule

TradeSight remains paper-only. Source consolidation does not authorize live-money
trading, credential changes, release publication, or deletion of historical copies.
