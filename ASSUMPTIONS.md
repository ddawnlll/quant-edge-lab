# Interpretation assumptions

The request says to implement and run the lab until complete, blocked, or
waiting on elapsed time. I interpreted "complete" as: every feasible mandatory
software component exists, is exercised through its real public interface, and
its evidence is recorded. A positive scientific promotion or live trading is
not implied and is explicitly gated.

Guessed or environment-constrained interpretations:

1. **"Complete through M5"** means the software and its local acceptance paths,
   not a certificate of edge, queue fidelity, or full venue parity.
2. **Services** run as the current `quant` user via user systemd with linger.
   Creating `qel-research`, `qel-eval`, and `qel-worker` Unix owners needs an
   administrator; that separation remains blocked rather than simulated.
3. **DuckDB** is named by the specification but crashes with an illegal
   instruction on this CPU. The normalization path uses PyArrow/Parquet and
   records the substitution instead of pretending DuckDB ran.
4. **Archive L1 quotes** are synthetic top-of-book assumptions around observed
   trades. They are labeled `assumed_synthetic_l1_around_trade`, never observed
   depth.
5. **Synthetic evaluator vectors** test the evaluator plumbing and refusal
   behavior. They are not market results and cannot promote a strategy.
6. **No real-money orders, paid calls, or new dependencies on paid services**
   were introduced. The public GitHub repository is the only external write.
