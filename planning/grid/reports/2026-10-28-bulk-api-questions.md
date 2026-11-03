# Bulk API verification questions

Prepared for review; not sent. No private keys or account identifiers are included.

## History coverage

On 2026-10-28 at approximately 12:23 UTC, two unsigned mainnet `POST /api/v1/account` history requests, one each for `fills` and `fundingHistory`, used `startSlot: 597042200` and `endSlot: 597042203` for an existing master account. Each returned HTTP 200, an empty `data` array, `hasMore: false`, `asOfSlot: 597042203`, the requested bounds, `coverage: "unknown"`, and no `minAvailableSlot`. These observations are limited to that account and window; they do not establish the account's current position or completeness of history.

The [account API documentation](https://docs.bulk.trade/api-reference/getAccount) illustrates `coverage: "complete"` and `minAvailableSlot`. What conditions cause a valid bounded mainnet query to return `unknown` without a retention floor? Is there an authoritative read-only way to establish complete coverage for a specific account and slot interval? If availability depends on the backend, endpoint, retention, account activity, or indexing lag, what are the supported detection and retry rules?

## Stop trigger and execution association

The [order API](https://docs.bulk.trade/api-reference/placeOrder) identifies `triggered` as a non-terminal conditional-root status. The [conditional-order documentation](https://docs.bulk.trade/bulk-exchange/conditional-orders) says a Stop submits a reduce-only order on trigger. Which immutable fields link a Stop root to its execution order and all resulting fills? Which native states and history-coverage guarantees prove that the triggered order can no longer execute or generate delayed fills? Please provide an example covering partial execution, trigger failure and cancellation races.

The order API describes `partiallyFilled` as terminal, while the [official Python SDK HTTP reference](https://github.com/Bulk-trade/bulk-client/blob/main/docs/python-http-api.md) describes it as leaving the remainder resting. Which description applies to the current mainnet protocol and each order policy? Is the SDK text stale, or can the same status be active in another context? Please include authoritative examples and the expected corresponding open-order and history responses.

Further observation: the published Python wheel `bulk-client==0.1.2` (SHA-256 `f911bc688583c9974e0610f0a9035199941a3351c705589d6c793788c498daae`) maps `partiallyFilled` to enum value 4, so both its `is_terminal()` and `is_placed()` predicates evaluate true. This is package implementation evidence, not proof of current exchange state or intended semantics. The wheel was inspected without installing or executing it.

## Pre-trade risk admission

Is there a documented read-only risk simulation or canonical implementation that evaluates an entire account portfolio including existing and proposed open orders at a specified consistent state? If not, please specify the order admission calculation, precision and rounding rules, relevant snapshot/version semantics and how to obtain a conservative upper bound across proposed grid orders. The [risk surface](https://docs.bulk.trade/api-reference/getRiskSurfaces) alone is not an account-level admission decision.

The questions request technical information only; no message has been sent.

### Small-position leverage floor observation (2026-10-29)

A preserved mainnet single-position sample from 2026-10-26 reports BTC quantity `0.0000188`, position `leverage: 40.0`, `lambda: 0.025`, account notional `1.495014258` and `marginUsed: 0.03737535645`. The ratio is exactly `0.025`, which also equals `1 / 40`. A public BTC risk-surface read on 2026-10-29 still has a minimum notional knot of `50000` and minimum portfolio-leverage knot of `1`; its first buy/sell origin and equilibrium rates are `0.02` in every regime. These observations are from different times; they are not a synchronized replay.

Does the configured per-market leverage impose a `1/leverage` lambda floor? What are the exact rules below the minimum notional/portfolio-leverage knots, and how is that floor combined with the portfolio surface and liquidation reserve? The matching single-position arithmetic is only a hypothesis, not evidence that the same rule covers pending orders, multiple markets or future configurations.

## Testnet faucet unconfirmed result

An isolated new testnet identity submitted one signed faucet action to the official testnet `/api/v1/order` endpoint. The original request had a unique nanosecond nonce and the testnet signing domain. HTTP returned 200, but the expected `deposit` receipt was not confirmed; the response body was not preserved. Subsequent `fullAccount` and `activityHistory` reads for the same identity returned HTTP 404 `ACCOUNT_NOT_FOUND`. There was no repeat submission.

Is there a supported read-only way to retrieve the outcome of a specific original signed faucet request using the account and nonce? What response or backend evidence can definitively establish that such a request will not be applied later? The public [faucet documentation](https://docs.bulk.trade/api-reference/requestFaucet) describes a nanosecond nonce as supported; the result alone does not identify the cause of the unconfirmed receipt.
