use qel_policy::{parse_strategy, Engine, Val};
use serde_json::Value;
use std::collections::HashMap;
use std::path::PathBuf;

fn strategy() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../specification/examples/strategy_mr.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-12 + 1e-10 * b.abs()
}

#[test]
fn validates_example_and_warmup() {
    let s = parse_strategy(&strategy()).unwrap();
    assert_eq!(s.min_bars, 144);
    assert_eq!(s.nodes.len(), 21);
}

#[test]
fn reference_features_and_no_early_decision() {
    let mut engine = Engine::new(parse_strategy(&strategy()).unwrap());
    let mut checked = false;
    for i in 0..200 {
        let mid = 100.0 + (i as f64 / 10.0).sin() * 5.0;
        let mut inputs = HashMap::new();
        inputs.insert("MID_CLOSE".to_string(), Some(mid));
        let out = engine.step(&inputs, (i as u64) * 900_000_000_000).unwrap();
        if i + 1 < 144 {
            assert!(!out.decision_enabled, "decision at bar {}", i + 1);
        }
        if i + 1 == 144 {
            checked = true;
            assert!(out.decision_enabled);
            let ema = match engine.values.get("N-EMA") { Some(Val::F(v)) => *v, _ => panic!() };
            let std = match engine.values.get("N-STD") { Some(Val::F(v)) => *v, _ => panic!() };
            let sigma = match engine.values.get("N-SIGMA") { Some(Val::F(v)) => *v, _ => panic!() };
            assert!(close(ema, 4.615789270739427), "ema {ema}");
            assert!(close(std, 0.0327894893700445), "std {std}");
            assert!(close(sigma, 0.033145788661453526), "sigma {sigma}");
        }
    }
    assert!(checked);
}

fn mutated(f: impl FnOnce(&mut Value)) -> Value {
    let mut v = strategy();
    f(&mut v);
    v
}

#[test]
fn leakage_and_type_attacks_rejected() {
    assert!(parse_strategy(&mutated(|v| v["feature_dag"][2]["params"]["centered"] = Value::Bool(true))).is_err());
    assert!(parse_strategy(&mutated(|v| v["feature_dag"][0]["params"]["field"] = Value::String("FUTURE_CLOSE".into()))).is_err());
    assert!(parse_strategy(&mutated(|v| v["feature_dag"][0]["future"] = Value::Bool(true))).is_err());
    assert!(parse_strategy(&mutated(|v| v["feature_dag"][2]["params"]["window"] = Value::from(0))).is_err());
    assert!(parse_strategy(&mutated(|v| v["feature_dag"][1]["inputs"] = serde_json::json!(["N-MID", "N-EMA"]))).is_err());
    assert!(parse_strategy(&mutated(|v| v["feature_dag"][0]["operator_id"] = Value::String("OP-SCALER".into()))).is_err());
}
