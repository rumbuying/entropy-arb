# Official legacy Stop order-hash oracle

This public-only helper is pinned to official bulk-client revision
`303de5a8407c6c00ba8eaac2d1fa84dfabc4233c`, before optional builder/slippage
fields extended the conditional serializer. It compiles and calls that
revision's actual `Action.hash()` and `StopOrTP` serializer; no key or network
operation is part of the executable.

The 2026-10-26 mainnet stop probe demonstrated that V4 signing bytes and native
Stop order-hash bytes differ. Signing includes absent builder/slippage tags;
the deployed root ID retains the official six-field schema. The previous
latest-source oracle matched SDK signing bytes but did not match that native
root ID. The legacy official helper exactly reproduces the root seen and
cancelled on mainnet, using the original public action/account/nonce.

Build: isolated macOS Rust1.91.1, offline and locked, cached target only.
Receipt: `data/run/grid-bulk-stop-legacy-build-v1.json` (exit0).
Binary SHA256: `358bdc4914a3de60225e9985d06e4cfe721be0f1acc4de51c5096c1961a2a0d4`.
The executable is outside the repository/runtime installation:
`data/staging/bulk-native-identity-build-v2/target/release/entropy-bulk-stop-legacy-identity`.

Public 36-case output: `tests/fixtures/grid_bulk/stop-order-hash-legacy-20261026.json`.
Actual original-root comparison: `data/run/grid-bulk-stop-legacy-native-root-proof-v1.json`
(private audit file; no signing key). The portable SDK provider checks the full
V4 shape, derives only the six established hash fields, and retains complete
V4 signing bytes. It does not truncate an order ID or change a journal nonce.

Current Linux proof: `data/run/grid-bulk-stop-id-fix-linux-v2.json` (353 tests;
actual SDK36 cases against both official serializers). This is evidence for
encoding, one actual zero-position stop and its cancellation. It is not held
protection, complete history coverage, grid acceptance or ordinary live launch.
