# Bulk V4 canonical-byte oracle

This helper calls official bulk-client revision
`567dbc51a38c73d757ccb91547f197215b40ddc5`, with its actual serializers and
Action.hash(). The compiled macOS binary remains useful for verifying current
SDK V4 signing bytes. **Its Stop hash is not the deployed conditional root ID.**

A real mainnet stop on 2026-10-26 showed the mismatch. The node retains the
six-field Stop order-hash schema; V4 signing adds absent builder/slippage tags.
Independent official older-source compilation exactly reproduces the accepted
native root. See `../bulk-stop-legacy-identity/README.md` for that oracle and
current native evidence. Earlier reports matching only this V4 Action.hash()
remain historical signing/serializer proofs and must not certify root IDs.

Original isolated Mac Rust1.91.1/offline/locked build:
`data/staging/bulk-native-identity-build-v2/native-build-v4.json`, exit0.
Binary SHA256: `884c46ef27b500eedf267ec95f3aae80824e5b839be04532bdfcfb3560f1cdb4`.
The global compiler and production environments were not changed.

Current `BulkSdkStopIdentity` checks the complete current SDK V4 message and
uses the independently verified legacy hash fields for the native root ID.
`BulkNativeIdentity` validates this executable's exact output for canonical
provenance, then applies that same legacy hash schema. Neither accepts a key.
The engine still owns the original CID/nonce; a migration keeps the original
wrong computed ID/unknown receipt and appends positive terminal resolution.

The SDK checker now requires **both** `--oracle` and `--hash-oracle`. Current
Linux actual SDK36 proof and full Bulk353 tests are in
`data/run/grid-bulk-stop-id-fix-linux-v2.json`. No mandatory Rust service is
installed. These tests and the one stop/cancel do not certify held protection,
trigger-child linkage, continuous budget/risk, complete history or live grids.
