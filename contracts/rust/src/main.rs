mod canonical;
mod schema;

use anyhow::{bail, Context, Result};
use std::io::{self, Read};

fn read_stdin() -> Result<serde_json::Value> {
    let mut text = String::new();
    io::stdin().read_to_string(&mut text)?;
    Ok(serde_json::from_str(&text)?)
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        bail!("usage: qel-contract-rs canonical|hash|runkey|validate|selftest [--def NAME --file PATH]");
    }
    match args[1].as_str() {
        "canonical" => {
            let value = read_stdin()?;
            println!("{}", String::from_utf8(canonical::canonical_bytes(&value)?)?);
        }
        "hash" => {
            let value = read_stdin()?;
            println!("{}", canonical::hash_jcs(&value)?);
        }
        "runkey" => {
            let value = read_stdin()?;
            let map = value.as_object().context("runkey material must be an object")?;
            println!("{}", canonical::run_key(map)?);
        }
        "validate" => {
            let mut def = None;
            let mut file = None;
            let mut i = 2;
            while i < args.len() {
                match args[i].as_str() {
                    "--def" => {
                        def = args.get(i + 1).cloned();
                        i += 2;
                    }
                    "--file" => {
                        file = args.get(i + 1).cloned();
                        i += 2;
                    }
                    other => bail!("unknown argument {other}"),
                }
            }
            let def = def.context("--def is required")?;
            let value: serde_json::Value = if let Some(path) = file {
                serde_json::from_str(&std::fs::read_to_string(path)?)?
            } else {
                read_stdin()?
            };
            schema::validate_def(&def, &value)?;
            println!("VALID {def}");
        }
        "selftest" => {
            let vectors = serde_json::json!({"a": "9007199254740993", "b": "1.25", "c": null});
            let bytes = canonical::canonical_bytes(&vectors)?;
            let hash = canonical::hash_jcs(&vectors)?;
            let want = r#"{"a":"9007199254740993","b":"1.25","c":null}"#;
            let vector_pass = bytes == want.as_bytes()
                && hash == "129712fa856798af9106eb65d951f98ef56459ae40d587552cd7a4b162b7ae6e";
            let mut rejected = 0;
            for bad in ["-0", "01", "1e3"] {
                if canonical::parse_decimal(bad).is_err() {
                    rejected += 1;
                }
            }
            if canonical::parse_int128("170141183460469231731687303715884105728").is_err() {
                rejected += 1;
            }
            if canonical::parse_decimal("1.230").is_err() {
                rejected += 1;
            }
            println!(
                "{}",
                serde_json::json!({
                    "canonical_utf8": String::from_utf8(bytes)?,
                    "sha256": hash,
                    "vector_pass": vector_pass,
                    "rejected": rejected
                })
            );
            if !vector_pass || rejected != 5 {
                std::process::exit(2);
            }
        }
        other => bail!("unknown command {other}"),
    }
    Ok(())
}
