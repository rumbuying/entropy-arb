//! Public-only native identity bridge. There is no key, signer or HTTP call.
use bulk_client::transaction::{Action, ActionMeta};
use serde::Deserialize;
use solana_pubkey::Pubkey;
use std::io::{self, Read};
use std::str::FromStr;

const REV: &str = "567dbc51a38c73d757ccb91547f197215b40ddc5";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    action: serde_json::Value,
    account: String,
    nonce: u64,
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut bytes = Vec::new();
    io::stdin().take(16385).read_to_end(&mut bytes)?;
    if bytes.len() > 16384 { return Err("input too large".into()); }
    let input: Input = serde_json::from_slice(&bytes)?;
    let mut action: Action = serde_json::from_value(input.action)?;
    // The first release needs ordinary limits and fixed-size cross stops.
    // An unknown variant must not quietly authorize unrelated actions.
    match &action {
        Action::LimitOrder(o) if !o.iso => {},
        Action::Stop(s) if !s.iso && s.builder_code.is_none() && s.slippage.is_none()
            && s.limit.is_none() && s.size.is_finite() && s.size > 0.0
            && s.threshold.is_finite() && s.threshold > 0.0 => {},
        _ => return Err("unsupported identity action".into()),
    }
    let canonical = bincode::serialize(&action)?;
    action.link(ActionMeta {
        account: Pubkey::from_str(&input.account)?, nonce: input.nonce,
        seqno: 0, hash: None,
    });
    let oid = action.hash().to_string();
    let hex: String = canonical.iter().map(|byte| format!("{byte:02x}")).collect();
    println!("{}", serde_json::json!({
        "order_id": oid, "canonical_action_hex": hex, "source_revision": REV,
        "account": input.account, "nonce": input.nonce, "action_index": 0
    }));
    Ok(())
}

fn main() {
    if run().is_err() {
        // Never echo stdin: callers pass public actions only, but errors
        // should remain bounded and independent of arbitrary caller text.
        eprintln!("BULK_NATIVE_IDENTITY_REFUSED");
        std::process::exit(2);
    }
}
