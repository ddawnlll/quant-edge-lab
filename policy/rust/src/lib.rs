//! QEL typed feature DAG and intent-only policy evaluator.

use anyhow::{anyhow, bail, Result};
use serde_json::Value;
use std::collections::{HashMap, VecDeque};

#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    F(f64),
    B(bool),
    Null,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Ty {
    F64,
    Bool,
}

pub struct OpDef {
    pub id: &'static str,
    pub inputs: &'static [Ty],
    pub output: Ty,
    pub stateful: bool,
}

pub const OPS: &[OpDef] = &[
    OpDef { id: "OP-ADD", inputs: &[Ty::F64, Ty::F64], output: Ty::F64, stateful: false },
    OpDef { id: "OP-SUB", inputs: &[Ty::F64, Ty::F64], output: Ty::F64, stateful: false },
    OpDef { id: "OP-MUL", inputs: &[Ty::F64, Ty::F64], output: Ty::F64, stateful: false },
    OpDef { id: "OP-DIV", inputs: &[Ty::F64, Ty::F64], output: Ty::F64, stateful: false },
    OpDef { id: "OP-LOG", inputs: &[Ty::F64], output: Ty::F64, stateful: false },
    OpDef { id: "OP-EXP", inputs: &[Ty::F64], output: Ty::F64, stateful: false },
    OpDef { id: "OP-LT", inputs: &[Ty::F64, Ty::F64], output: Ty::Bool, stateful: false },
    OpDef { id: "OP-LE", inputs: &[Ty::F64, Ty::F64], output: Ty::Bool, stateful: false },
    OpDef { id: "OP-GT", inputs: &[Ty::F64, Ty::F64], output: Ty::Bool, stateful: false },
    OpDef { id: "OP-GE", inputs: &[Ty::F64, Ty::F64], output: Ty::Bool, stateful: false },
    OpDef { id: "OP-AND", inputs: &[Ty::Bool, Ty::Bool], output: Ty::Bool, stateful: false },
    OpDef { id: "OP-OR", inputs: &[Ty::Bool, Ty::Bool], output: Ty::Bool, stateful: false },
    OpDef { id: "OP-NOT", inputs: &[Ty::Bool], output: Ty::Bool, stateful: false },
    OpDef { id: "OP-ABS", inputs: &[Ty::F64], output: Ty::F64, stateful: false },
    OpDef { id: "OP-CONST", inputs: &[], output: Ty::F64, stateful: false },
    OpDef { id: "OP-INPUT", inputs: &[], output: Ty::F64, stateful: false },
    OpDef { id: "OP-LAG", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-SMA", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-SUM", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-MIN", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-MAX", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-EMA", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-STD", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-ER", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-PIVOT", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
    OpDef { id: "OP-QUANTILE", inputs: &[Ty::F64], output: Ty::F64, stateful: true },
];

pub fn op_def(id: &str) -> Option<&'static OpDef> {
    OPS.iter().find(|o| o.id == id)
}

#[derive(Clone, Debug)]
pub struct Node {
    pub id: String,
    pub op: String,
    pub inputs: Vec<String>,
    pub params: HashMap<String, Value>,
    pub output: Ty,
    pub clock: String,
    pub null_policy: String,
    pub fit_scope: String,
}

#[derive(Clone, Debug)]
pub struct Strategy {
    pub nodes: Vec<Node>,
    pub index: HashMap<String, usize>,
    pub min_bars: usize,
    pub long_node: String,
    pub short_node: String,
    pub long_exit: String,
    pub short_exit: String,
    pub cooldown_ns: u64,
    pub max_active_orders: usize,
    pub required_capabilities: Vec<String>,
}

fn int_param(node: &Node, key: &str) -> Result<i64> {
    node.params
        .get(key)
        .and_then(|v| v.as_i64())
        .ok_or_else(|| anyhow!("{} missing integer param {}", node.id, key))
}

fn check_params(node: &Node) -> Result<()> {
    let allowed: Vec<&str> = match node.op.as_str() {
        "OP-CONST" => vec!["value"],
        "OP-INPUT" => vec!["field"],
        "OP-LAG" => vec!["periods"],
        "OP-SMA" | "OP-SUM" | "OP-MIN" | "OP-MAX" | "OP-EMA" | "OP-ER" => vec!["window"],
        "OP-STD" => vec!["window", "ddof"],
        "OP-PIVOT" => vec!["left", "right", "kind"],
        "OP-QUANTILE" => vec!["window", "probability", "approximate"],
        _ => vec![],
    };
    for key in node.params.keys() {
        if !allowed.contains(&key.as_str()) {
            bail!("{} unknown parameter {}", node.id, key);
        }
    }
    match node.op.as_str() {
        "OP-CONST" => {
            let value = node.params.get("value").and_then(|v| v.as_str()).ok_or_else(|| anyhow!("CONST value"))?;
            let parsed: f64 = value.parse().map_err(|_| anyhow!("CONST non-finite"))?;
            if !parsed.is_finite() {
                bail!("CONST non-finite");
            }
        }
        "OP-INPUT" => {
            let field = node.params.get("field").and_then(|v| v.as_str()).unwrap_or("");
            let allowed = ["MID_CLOSE", "TRADE_CLOSE", "BASE_VOLUME", "QUOTE_VOLUME", "MARK", "INDEX", "AVAILABLE_FUNDING_RATE"];
            if !allowed.contains(&field) {
                bail!("{} unknown input field {}", node.id, field);
            }
        }
        "OP-LAG" => {
            let p = int_param(node, "periods")?;
            if !(1..=10000).contains(&p) {
                bail!("{} periods outside 1..10000", node.id);
            }
        }
        "OP-SMA" | "OP-SUM" | "OP-MIN" | "OP-MAX" | "OP-EMA" | "OP-ER" | "OP-STD" | "OP-QUANTILE" => {
            let w = int_param(node, "window")?;
            if !(1..=10000).contains(&w) {
                bail!("{} window outside 1..10000", node.id);
            }
        }
        "OP-PIVOT" => {
            let l = int_param(node, "left")?;
            let r = int_param(node, "right")?;
            if !(0..=10000).contains(&l) || !(0..=10000).contains(&r) || l + r == 0 {
                bail!("{} pivot bounds invalid", node.id);
            }
            if !["HIGH", "LOW"].contains(&node.params.get("kind").and_then(|v| v.as_str()).unwrap_or("")) {
                bail!("{} pivot kind invalid", node.id);
            }
        }
        _ => {}
    }
    Ok(())
}

fn required_bars(node: &Node, reqs: &HashMap<String, usize>) -> usize {
    let input_req = node.inputs.iter().map(|i| *reqs.get(i).unwrap_or(&0)).max().unwrap_or(0);
    match node.op.as_str() {
        "OP-LAG" => input_req.max(1) + (int_param(node, "periods").unwrap_or(1) as usize).saturating_sub(1),
        "OP-SMA" | "OP-SUM" | "OP-MIN" | "OP-MAX" | "OP-EMA" | "OP-STD" | "OP-QUANTILE" => {
            input_req + int_param(node, "window").unwrap_or(0) as usize
        }
        "OP-ER" => input_req + int_param(node, "window").unwrap_or(0) as usize + 1,
        "OP-PIVOT" => {
            input_req
                + int_param(node, "left").unwrap_or(0) as usize
                + int_param(node, "right").unwrap_or(0) as usize
                + 1
        }
        _ => input_req,
    }
}

pub fn parse_strategy(value: &Value) -> Result<Strategy> {
    let dag = value["feature_dag"].as_array().ok_or_else(|| anyhow!("feature_dag missing"))?;
    if dag.is_empty() || dag.len() > 256 {
        bail!("feature_dag size");
    }
    let mut nodes = Vec::new();
    let mut index = HashMap::new();
    let mut reqs = HashMap::new();
    let mut stateful = 0;
    for raw in dag {
        let allowed_node_fields = ["node_id", "operator_id", "inputs", "params", "output_type", "fit_scope", "null_policy", "clock"];
        if let Some(obj) = raw.as_object() {
            for key in obj.keys() {
                if !allowed_node_fields.contains(&key.as_str()) {
                    bail!("unknown node field {key}");
                }
            }
        }
        let id = raw["node_id"].as_str().ok_or_else(|| anyhow!("node_id"))?.to_string();
        if index.contains_key(&id) {
            bail!("duplicate node {id}");
        }
        let op = raw["operator_id"].as_str().ok_or_else(|| anyhow!("operator_id"))?.to_string();
        let def = op_def(&op).ok_or_else(|| anyhow!("unknown operator {op}"))?;
        if def.stateful {
            stateful += 1;
        }
        let inputs: Vec<String> = raw["inputs"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default();
        if inputs.len() != def.inputs.len() {
            bail!("{id} arity");
        }
        for input in &inputs {
            let Some(&idx) = index.get(input) else { bail!("{id} forward/undefined input {input}") };
            let ty = nodes.get::<usize>(idx).map(|n: &Node| n.output).unwrap();
            // input type validation happens below with the corresponding operator slot
            let _ = ty;
        }
        let output = match raw["output_type"].as_str().unwrap_or("") {
            "F64" => Ty::F64,
            "BOOL" => Ty::Bool,
            other => bail!("{id} output_type {other}"),
        };
        if output != def.output {
            bail!("{id} output type mismatch");
        }
        let params: HashMap<String, Value> = raw["params"].as_object().map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect()).unwrap_or_default();
        let node = Node {
            id: id.clone(),
            op: op.clone(),
            inputs: inputs.clone(),
            params,
            output,
            clock: raw["clock"].as_str().unwrap_or("").to_string(),
            null_policy: raw["null_policy"].as_str().unwrap_or("").to_string(),
            fit_scope: raw["fit_scope"].as_str().unwrap_or("").to_string(),
        };
        check_params(&node)?;
        for (slot, input) in def.inputs.iter().zip(inputs.iter()) {
            let idx = index[input];
            if nodes[idx].output != *slot {
                bail!("{id} input type mismatch for {input}");
            }
        }
        if !["MARKET_DELIVERY", "BAR_CLOSE_DELIVERY", "ORDER_RESPONSE"].contains(&node.clock.as_str()) {
            bail!("{id} clock");
        }
        if !["PROPAGATE_ABSTAIN", "FALSE_WITH_REASON"].contains(&node.null_policy.as_str()) {
            bail!("{id} null_policy");
        }
        if !["NONE", "TRAIN_ONLY"].contains(&node.fit_scope.as_str()) {
            bail!("{id} fit_scope");
        }
        reqs.insert(id.clone(), required_bars(&node, &reqs));
        index.insert(id.clone(), nodes.len());
        nodes.push(node);
    }
    if stateful > 32 {
        bail!("too many stateful operators");
    }
    if let Some(max_stateful) = value["max_stateful_operators"].as_u64() {
        if stateful > max_stateful as usize {
            bail!("max_stateful_operators exceeded");
        }
    }
    if let Some(free) = value["free_search_parameters"].as_array() {
        if free.len() > 4 {
            bail!("free_search_parameters exceeds 4");
        }
    }
    let entry = &value["entry_policy"];
    let exit = &value["exit_policy"];
    let cooldown = entry["cooldown_ns"].as_str().unwrap_or("0").parse::<u64>()?;
    let min_bars = reqs.values().copied().max().unwrap_or(0);
    let required_capabilities = value["required_capabilities"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default();
    Ok(Strategy {
        nodes,
        index,
        min_bars,
        long_node: entry["long_condition_node"].as_str().unwrap_or("").to_string(),
        short_node: entry["short_condition_node"].as_str().unwrap_or("").to_string(),
        long_exit: exit["long_exit_node"].as_str().unwrap_or("").to_string(),
        short_exit: exit["short_exit_node"].as_str().unwrap_or("").to_string(),
        cooldown_ns: cooldown,
        max_active_orders: value["max_active_orders"].as_u64().unwrap_or(1) as usize,
        required_capabilities,
    })
}

enum State {
    None,
    Lag(VecDeque<Option<f64>>, usize),
    Window(VecDeque<Option<f64>>, usize, String),
    Ema { window: usize, sum: f64, count: usize, value: Option<f64> },
    Er(VecDeque<Option<f64>>, usize),
    Pivot(VecDeque<Option<f64>>, usize, usize, bool),
}

pub struct Engine {
    pub strategy: Strategy,
    states: Vec<State>,
    pub bars: usize,
    last_decision: Option<u64>,
    pub values: HashMap<String, Val>,
}

#[derive(Clone, Debug)]
pub struct StepOutput {
    pub decision_enabled: bool,
    pub long: bool,
    pub short: bool,
    pub long_exit: bool,
    pub short_exit: bool,
}

impl Engine {
    pub fn new(strategy: Strategy) -> Self {
        let states = strategy
            .nodes
            .iter()
            .map(|n| match n.op.as_str() {
                "OP-LAG" => State::Lag(VecDeque::new(), n.params.get("periods").and_then(|v| v.as_u64()).unwrap_or(1) as usize),
                "OP-SMA" | "OP-SUM" | "OP-MIN" | "OP-MAX" | "OP-STD" | "OP-QUANTILE" => State::Window(
                    VecDeque::new(),
                    n.params.get("window").and_then(|v| v.as_u64()).unwrap_or(1) as usize,
                    n.op.clone(),
                ),
                "OP-EMA" => State::Ema {
                    window: n.params.get("window").and_then(|v| v.as_u64()).unwrap_or(1) as usize,
                    sum: 0.0,
                    count: 0,
                    value: None,
                },
                "OP-ER" => State::Er(VecDeque::new(), n.params.get("window").and_then(|v| v.as_u64()).unwrap_or(1) as usize),
                "OP-PIVOT" => State::Pivot(
                    VecDeque::new(),
                    n.params.get("left").and_then(|v| v.as_u64()).unwrap_or(0) as usize,
                    n.params.get("right").and_then(|v| v.as_u64()).unwrap_or(0) as usize,
                    n.params.get("kind").and_then(|v| v.as_str()) == Some("HIGH"),
                ),
                _ => State::None,
            })
            .collect();
        Self { strategy, states, bars: 0, last_decision: None, values: HashMap::new() }
    }

    fn input_value(field: &str, inputs: &HashMap<String, Option<f64>>) -> Val {
        match inputs.get(field).copied().flatten() {
            Some(v) if v.is_finite() => Val::F(v),
            _ => Val::Null,
        }
    }

    fn val(&self, id: &str) -> Val {
        self.values.get(id).cloned().unwrap_or(Val::Null)
    }

    fn f(&self, id: &str) -> Option<f64> {
        match self.val(id) {
            Val::F(v) => Some(v),
            _ => None,
        }
    }

    fn b(&self, id: &str) -> Option<bool> {
        match self.val(id) {
            Val::B(v) => Some(v),
            _ => None,
        }
    }

    pub fn step(&mut self, inputs: &HashMap<String, Option<f64>>, now_ns: u64) -> Result<StepOutput> {
        self.bars += 1;
        for i in 0..self.strategy.nodes.len() {
            let node = self.strategy.nodes[i].clone();
            let value = match node.op.as_str() {
                "OP-INPUT" => Self::input_value(node.params.get("field").and_then(|v| v.as_str()).unwrap_or(""), inputs),
                "OP-CONST" => {
                    let text = node.params.get("value").and_then(|v| v.as_str()).unwrap_or("0");
                    Val::F(text.parse::<f64>()?)
                }
                "OP-ADD" => bin_f(self.f(&node.inputs[0]), self.f(&node.inputs[1]), |a, b| a + b),
                "OP-SUB" => bin_f(self.f(&node.inputs[0]), self.f(&node.inputs[1]), |a, b| a - b),
                "OP-MUL" => bin_f(self.f(&node.inputs[0]), self.f(&node.inputs[1]), |a, b| a * b),
                "OP-DIV" => bin_f(self.f(&node.inputs[0]), self.f(&node.inputs[1]), |a, b| if b == 0.0 { f64::NAN } else { a / b }),
                "OP-LOG" => un_f(self.f(&node.inputs[0]), |a| if a > 0.0 { a.ln() } else { f64::NAN }),
                "OP-EXP" => un_f(self.f(&node.inputs[0]), |a| a.exp()),
                "OP-ABS" => un_f(self.f(&node.inputs[0]), |a| a.abs()),
                "OP-NOT" => match self.b(&node.inputs[0]) {
                    Some(v) => Val::B(!v),
                    None => Val::Null,
                },
                "OP-LT" | "OP-LE" | "OP-GT" | "OP-GE" => {
                    let (Some(a), Some(b)) = (self.f(&node.inputs[0]), self.f(&node.inputs[1])) else { self.values.insert(node.id.clone(), Val::Null); continue };
                    Val::B(match node.op.as_str() {
                        "OP-LT" => a < b,
                        "OP-LE" => a <= b,
                        "OP-GT" => a > b,
                        _ => a >= b,
                    })
                }
                "OP-AND" | "OP-OR" => {
                    let (Some(a), Some(b)) = (self.b(&node.inputs[0]), self.b(&node.inputs[1])) else { self.values.insert(node.id.clone(), Val::Null); continue };
                    Val::B(if node.op == "OP-AND" { a && b } else { a || b })
                }
                "OP-LAG" => {
                    let input = self.f(&node.inputs[0]);
                    if let State::Lag(buf, periods) = &mut self.states[i] {
                        buf.push_back(input);
                        if buf.len() > *periods {
                            let out = buf.pop_front().flatten();
                            self.values.insert(node.id.clone(), out.map_or(Val::Null, Val::F));
                            continue;
                        }
                    }
                    Val::Null
                }
                "OP-SMA" | "OP-SUM" | "OP-MIN" | "OP-MAX" | "OP-STD" | "OP-QUANTILE" => {
                    let input = self.f(&node.inputs[0]);
                    if let State::Window(buf, window, kind) = &mut self.states[i] {
                        buf.push_back(input);
                        while buf.len() > *window {
                            buf.pop_front();
                        }
                        if buf.len() < *window || buf.iter().any(|v| v.is_none()) {
                            Val::Null
                        } else {
                            let vals: Vec<f64> = buf.iter().map(|v| v.unwrap()).collect();
                            match kind.as_str() {
                                "OP-SMA" => Val::F(vals.iter().sum::<f64>() / vals.len() as f64),
                                "OP-SUM" => Val::F(vals.iter().sum()),
                                "OP-MIN" => Val::F(vals.iter().copied().fold(f64::INFINITY, f64::min)),
                                "OP-MAX" => Val::F(vals.iter().copied().fold(f64::NEG_INFINITY, f64::max)),
                                "OP-STD" => {
                                    let n = vals.len() as f64;
                                    let mean = vals.iter().sum::<f64>() / n;
                                    let var = vals.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0);
                                    Val::F(var.sqrt())
                                }
                                _ => {
                                    let p: f64 = node.params.get("probability").and_then(|v| v.as_str()).and_then(|s| s.parse().ok()).unwrap_or(0.5);
                                    let mut sorted = vals.clone();
                                    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
                                    let h = (sorted.len() - 1) as f64 * p;
                                    let lo = h.floor() as usize;
                                    let hi = h.ceil() as usize;
                                    Val::F(sorted[lo] + (h - lo as f64) * (sorted[hi] - sorted[lo]))
                                }
                            }
                        }
                    } else {
                        Val::Null
                    }
                }
                "OP-EMA" => {
                    let input = self.f(&node.inputs[0]);
                    if let State::Ema { window, sum, count, value } = &mut self.states[i] {
                        let Some(x) = input else {
                            *sum = 0.0;
                            *count = 0;
                            *value = None;
                            self.values.insert(node.id.clone(), Val::Null);
                            continue;
                        };
                        if value.is_none() {
                            *sum += x;
                            *count += 1;
                            if *count >= *window {
                                *value = Some(*sum / *window as f64);
                            }
                        } else {
                            let alpha = 2.0 / (*window as f64 + 1.0);
                            *value = Some(alpha * x + (1.0 - alpha) * value.unwrap());
                        }
                        self.values.insert(node.id.clone(), value.map_or(Val::Null, Val::F));
                        continue;
                    }
                    Val::Null
                }
                "OP-ER" => {
                    let input = self.f(&node.inputs[0]);
                    if let State::Er(buf, window) = &mut self.states[i] {
                        buf.push_back(input);
                        while buf.len() > *window + 1 {
                            buf.pop_front();
                        }
                        if buf.len() < *window + 1 || buf.iter().any(|v| v.is_none()) {
                            Val::Null
                        } else {
                            let vals: Vec<f64> = buf.iter().map(|v| v.unwrap()).collect();
                            let num = (vals[vals.len() - 1] - vals[0]).abs();
                            let den: f64 = vals.windows(2).map(|w| (w[1] - w[0]).abs()).sum();
                            if den == 0.0 { Val::Null } else { Val::F(num / den) }
                        }
                    } else {
                        Val::Null
                    }
                }
                "OP-PIVOT" => {
                    let input = self.f(&node.inputs[0]);
                    if let State::Pivot(buf, left, right, high) = &mut self.states[i] {
                        buf.push_back(input);
                        while buf.len() > *left + *right + 1 {
                            buf.pop_front();
                        }
                        if buf.len() < *left + *right + 1 || buf.iter().any(|v| v.is_none()) {
                            Val::Null
                        } else {
                            let vals: Vec<f64> = buf.iter().map(|v| v.unwrap()).collect();
                            let mid = *left;
                            let is_pivot = if *high {
                                (0..mid).all(|j| vals[j] < vals[mid]) && (mid + 1..vals.len()).all(|j| vals[j] < vals[mid])
                            } else {
                                (0..mid).all(|j| vals[j] > vals[mid]) && (mid + 1..vals.len()).all(|j| vals[j] > vals[mid])
                            };
                            if is_pivot { Val::F(vals[mid]) } else { Val::Null }
                        }
                    } else {
                        Val::Null
                    }
                }
                other => bail!("unhandled operator {other}"),
            };
            self.values.insert(node.id.clone(), value);
        }
        let decision_enabled = self.bars >= self.strategy.min_bars;
        let long = self.b(&self.strategy.long_node).unwrap_or(false);
        let short = self.b(&self.strategy.short_node).unwrap_or(false);
        let long_exit = self.b(&self.strategy.long_exit).unwrap_or(false);
        let short_exit = self.b(&self.strategy.short_exit).unwrap_or(false);
        if decision_enabled && (long || short) {
            let allowed = match self.last_decision {
                None => true,
                Some(prev) => now_ns.saturating_sub(prev) >= self.strategy.cooldown_ns,
            };
            if !allowed {
                return Ok(StepOutput { decision_enabled, long: false, short: false, long_exit, short_exit });
            }
            self.last_decision = Some(now_ns);
        }
        Ok(StepOutput { decision_enabled, long, short, long_exit, short_exit })
    }
}

fn bin_f(a: Option<f64>, b: Option<f64>, f: impl Fn(f64, f64) -> f64) -> Val {
    match (a, b) {
        (Some(a), Some(b)) => {
            let v = f(a, b);
            if v.is_finite() { Val::F(v) } else { Val::Null }
        }
        _ => Val::Null,
    }
}

fn un_f(a: Option<f64>, f: impl Fn(f64) -> f64) -> Val {
    match a {
        Some(a) => {
            let v = f(a);
            if v.is_finite() { Val::F(v) } else { Val::Null }
        }
        None => Val::Null,
    }
}
