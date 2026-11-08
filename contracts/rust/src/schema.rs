//! Runtime schema validation for QEL contracts.

use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::path::PathBuf;
use unicode_normalization::UnicodeNormalization;

pub fn repo_root() -> Result<PathBuf> {
    if let Ok(root) = std::env::var("QEL_ROOT") {
        return Ok(PathBuf::from(root));
    }
    let mut dir = std::env::current_dir()?;
    loop {
        if dir.join("specification/schemas/runtime.schema.json").exists() {
            return Ok(dir);
        }
        if !dir.pop() {
            bail!("cannot locate QEL repository root");
        }
    }
}

pub fn runtime_schema() -> Result<Value> {
    let path = repo_root()?.join("specification/schemas/runtime.schema.json");
    let text = std::fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
    Ok(serde_json::from_str(&text)?)
}

fn resolve<'a>(schema: &'a Value, root: &'a Value) -> &'a Value {
    let mut current = schema;
    for _ in 0..32 {
        let Some(reference) = current.get("$ref").and_then(|v| v.as_str()) else {
            return current;
        };
        if !reference.starts_with("#/") {
            return current;
        }
        let mut node = root;
        for part in reference[2..].split('/') {
            let part = part.replace("~1", "/").replace("~0", "~");
            match node.get(&part) {
                Some(next) => node = next,
                None => return current,
            }
        }
        current = node;
    }
    current
}

fn validate_string(path: &str, text: &str, schema: &Value) -> Result<()> {
    if text.nfc().collect::<String>() != text {
        bail!("{path}: non-NFC text");
    }
    if let Some(domain) = schema.get("x-qel-integer") {
        let value = crate::canonical::parse_int128(text)
            .map_err(|e| anyhow!("{path}: {e}"))?;
        if let Some(min) = domain.get("minimum").and_then(|v| v.as_str()) {
            let min = crate::canonical::parse_int128(min)?;
            if value < min {
                bail!("{path}: integer below declared domain");
            }
        }
        if let Some(max) = domain.get("maximum").and_then(|v| v.as_str()) {
            let max = crate::canonical::parse_int128(max)?;
            if value > max {
                bail!("{path}: integer above declared domain");
            }
        }
    }
    if schema.get("x-qel-decimal").is_some() {
        crate::canonical::parse_decimal(text).map_err(|e| anyhow!("{path}: {e}"))?;
    }
    Ok(())
}

fn walk(instance: &Value, schema: &Value, root: &Value, path: &str) -> Result<()> {
    let schema = resolve(schema, root);
    if let Some(subs) = schema.get("allOf").and_then(|v| v.as_array()) {
        for sub in subs {
            walk(instance, sub, root, path)?;
        }
    }
    match instance {
        Value::Object(map) => {
            for (key, value) in map {
                if key.nfc().collect::<String>() != *key {
                    bail!("{path}: non-NFC key {key:?}");
                }
                if let Some(sub) = schema.get("properties").and_then(|p| p.get(key)) {
                    walk(value, sub, root, &format!("{path}.{key}"))?;
                } else if let Some(sub) = schema.get("additionalProperties").filter(|v| v.is_object()) {
                    walk(value, sub, root, &format!("{path}.{key}"))?;
                }
            }
        }
        Value::Array(items) => {
            if let Some(sub) = schema.get("items") {
                for (i, value) in items.iter().enumerate() {
                    walk(value, sub, root, &format!("{path}[{i}]"))?;
                }
            }
        }
        Value::String(text) => validate_string(path, text, schema)?,
        _ => {}
    }
    Ok(())
}

pub fn validate_def(def_name: &str, instance: &Value) -> Result<()> {
    let runtime = runtime_schema()?;
    let mut wrapper = serde_json::Map::new();
    for key in ["$schema", "$defs"] {
        if let Some(value) = runtime.get(key) {
            wrapper.insert(key.to_string(), value.clone());
        }
    }
    wrapper.insert(
        "$ref".to_string(),
        Value::String(format!("#/$defs/{def_name}")),
    );
    let wrapper = Value::Object(wrapper);
    let validator = jsonschema::validator_for(&wrapper).context("compile runtime schema")?;
    if let Some(error) = validator.iter_errors(instance).next() {
        bail!("{def_name}: {error}");
    }
    walk(instance, &wrapper, &runtime, "$")
}
