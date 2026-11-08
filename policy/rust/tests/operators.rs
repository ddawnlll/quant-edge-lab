use qel_policy::{parse_strategy, Engine, Val};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;

fn base() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../specification/examples/strategy_mr.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn node(id: &str, op: &str, inputs: &[&str], params: Value, ty: &str) -> Value {
    json!({
        "node_id": id,
        "operator_id": op,
        "inputs": inputs,
        "params": params,
        "output_type": ty,
        "fit_scope": "NONE",
        "null_policy": "PROPAGATE_ABSTAIN",
        "clock": "BAR_CLOSE_DELIVERY"
    })
}

fn engine(dag: Vec<Value>) -> Engine {
    let mut strategy = base();
    strategy["feature_dag"] = Value::Array(dag);
    Engine::new(parse_strategy(&strategy).unwrap())
}

fn input_dag(op: &str, params: Value) -> Vec<Value> {
    vec![
        node("N-IN", "OP-INPUT", &[], json!({"field":"MID_CLOSE"}), "F64"),
        node("N-OP", op, &["N-IN"], params, "F64"),
    ]
}

fn binary_dag(op: &str, constant: &str) -> Vec<Value> {
    vec![
        node("N-IN", "OP-INPUT", &[], json!({"field":"MID_CLOSE"}), "F64"),
        node("N-C", "OP-CONST", &[], json!({"value":constant}), "F64"),
        node("N-OP", op, &["N-IN", "N-C"], json!({}), "F64"),
    ]
}

fn feed(engine: &mut Engine, values: &[f64]) -> Vec<Val> {
    values
        .iter()
        .enumerate()
        .map(|(i, v)| {
            let mut inputs = HashMap::new();
            inputs.insert("MID_CLOSE".to_string(), Some(*v));
            engine.step(&inputs, (i as u64 + 1) * 900_000_000_000).unwrap();
            engine.values.get("N-OP").cloned().unwrap_or(Val::Null)
        })
        .collect()
}

fn f(v: Val) -> f64 {
    match v {
        Val::F(x) => x,
        other => panic!("expected F64, got {other:?}"),
    }
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-12 + 1e-10 * b.abs()
}

#[test]
fn arithmetic_and_unary_operators() {
    let cases = [
        ("OP-LOG", json!({}), vec![4.0], 4.0_f64.ln()),
        ("OP-EXP", json!({}), vec![1.0], 1.0_f64.exp()),
        ("OP-ABS", json!({}), vec![-2.0], 2.0),
    ];
    for (op, params, series, expected) in cases {
        let mut e = engine(input_dag(op, params));
        let out = feed(&mut e, &series);
        assert!(close(f(out.last().cloned().unwrap_or(Val::Null)), expected), "{op} = {:?}", out);
    }
    let cases = [
        ("OP-ADD", 3.0, 7.0),
        ("OP-SUB", 3.0, 1.0),
        ("OP-MUL", 3.0, 12.0),
        ("OP-DIV", 2.0, 2.0),
    ];
    for (op, constant, expected) in cases {
        let mut e = engine(binary_dag(op, &constant.to_string()));
        let out = feed(&mut e, &[4.0]);
        assert!(close(f(out.last().cloned().unwrap_or(Val::Null)), expected), "{op}");
    }
}

#[test]
fn window_operators_exact() {
    let cases = [
        ("OP-SMA", json!({"window":3}), 2.0),
        ("OP-SUM", json!({"window":3}), 6.0),
        ("OP-MIN", json!({"window":3}), 1.0),
        ("OP-MAX", json!({"window":3}), 3.0),
        ("OP-STD", json!({"window":3,"ddof":1}), 1.0),
        ("OP-QUANTILE", json!({"window":3,"probability":"0.5","approximate":false}), 2.0),
    ];
    for (op, params, expected) in cases {
        let mut e = engine(input_dag(op, params));
        let out = feed(&mut e, &[1.0, 2.0, 3.0]);
        assert_eq!(out[0], Val::Null);
        assert_eq!(out[1], Val::Null);
        assert!(close(f(out[2].clone()), expected), "{op} = {:?}", out[2]);
    }
}

#[test]
fn lag_ema_er_pivot_exact() {
    let mut lag = engine(input_dag("OP-LAG", json!({"periods":2})));
    let out = feed(&mut lag, &[1.0, 2.0, 3.0, 4.0]);
    assert_eq!(out[0], Val::Null);
    assert_eq!(out[1], Val::Null);
    assert!(close(f(out[2].clone()), 1.0));

    let mut ema = engine(input_dag("OP-EMA", json!({"window":3})));
    let out = feed(&mut ema, &[1.0, 2.0, 3.0, 4.0]);
    assert!(close(f(out[2].clone()), 2.0));
    assert!(close(f(out[3].clone()), 3.0));

    let mut er = engine(input_dag("OP-ER", json!({"window":2})));
    let out = feed(&mut er, &[1.0, 2.0, 3.0]);
    assert!(close(f(out[2].clone()), 1.0));

    let mut pivot = engine(input_dag("OP-PIVOT", json!({"left":1,"right":1,"kind":"HIGH"})));
    let out = feed(&mut pivot, &[1.0, 3.0, 2.0]);
    assert!(close(f(out[2].clone()), 3.0));

    let mut low = engine(input_dag("OP-PIVOT", json!({"left":1,"right":1,"kind":"LOW"})));
    let out = feed(&mut low, &[3.0, 1.0, 2.0]);
    assert!(close(f(out[2].clone()), 1.0));
}

#[test]
fn boolean_operators_exact() {
    let dag = vec![
        node("N-IN", "OP-INPUT", &[], json!({"field":"MID_CLOSE"}), "F64"),
        node("N-ONE", "OP-CONST", &[], json!({"value":"1"}), "F64"),
        node("N-THREE", "OP-CONST", &[], json!({"value":"3"}), "F64"),
        node("N-GT", "OP-GT", &["N-IN", "N-ONE"], json!({}), "BOOL"),
        node("N-LT", "OP-LT", &["N-IN", "N-THREE"], json!({}), "BOOL"),
        node("N-LE", "OP-LE", &["N-IN", "N-ONE"], json!({}), "BOOL"),
        node("N-GE", "OP-GE", &["N-IN", "N-ONE"], json!({}), "BOOL"),
        node("N-AND", "OP-AND", &["N-GT", "N-LT"], json!({}), "BOOL"),
        node("N-OR", "OP-OR", &["N-GT", "N-LT"], json!({}), "BOOL"),
        node("N-NOT", "OP-NOT", &["N-GT"], json!({}), "BOOL"),
    ];
    let mut e = engine(dag);
    let mut inputs = HashMap::new();
    inputs.insert("MID_CLOSE".to_string(), Some(2.0));
    e.step(&inputs, 1).unwrap();
    assert_eq!(e.values.get("N-AND"), Some(&Val::B(true)));
    assert_eq!(e.values.get("N-OR"), Some(&Val::B(true)));
    assert_eq!(e.values.get("N-NOT"), Some(&Val::B(false)));
    assert_eq!(e.values.get("N-LE"), Some(&Val::B(false)));
    assert_eq!(e.values.get("N-GE"), Some(&Val::B(true)));
}
