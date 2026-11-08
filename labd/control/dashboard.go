package control

const dashboardHTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Quant Edge Lab</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: dark; }
body { font: 14px/1.45 ui-monospace, monospace; margin: 0; background: #0c1016; color: #d6dde8; }
header { padding: 14px 18px; border-bottom: 1px solid #222b3a; display: flex; gap: 18px; align-items: baseline; }
h1 { font-size: 16px; margin: 0; font-weight: 600; }
main { padding: 18px; display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
section { border: 1px solid #222b3a; border-radius: 6px; padding: 12px 14px; background: #101722; }
h2 { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: #8fa3bf; margin: 0 0 10px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; }
dt { color: #7f93ad; }
dd { margin: 0; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid #1b2432; }
th { color: #8fa3bf; font-weight: 500; }
.ok { color: #78d7a5; } .warn { color: #e0c36a; } .bad { color: #e08383; }
#stamp { margin-left: auto; color: #7f93ad; font-size: 12px; }
</style>
</head>
<body>
<header><h1>QUANT EDGE LAB</h1><span id="health" class="warn">connecting</span><span id="stamp"></span></header>
<main>
<section><h2>Projection</h2><dl id="projection"></dl></section>
<section><h2>Queue</h2><dl id="queue"></dl></section>
<section style="grid-column: 1 / -1">
<h2>Ledger tail</h2>
<table><thead><tr><th>seq</th><th>type</th><th>principal</th><th>when</th><th>hash</th></tr></thead>
<tbody id="ledger"></tbody></table>
</section>
</main>
<script>
function row(dl, k, v, cls) {
  const dt = document.createElement('dt'); dt.textContent = k;
  const dd = document.createElement('dd'); dd.textContent = v == null ? '-' : String(v);
  if (cls) dd.className = cls;
  dl.append(dt, dd);
}
function render(d) {
  const p = document.getElementById('projection'); p.replaceChildren();
  row(p, 'ledger cursor', d.ledger_cursor);
  row(p, 'OOS state', d.oos_state);
  row(p, 'risk state', d.risk_state);
  row(p, 'alpha next', d.alpha_next.num + '/' + d.alpha_next.den);
  row(p, 'trading net', d.trading_net_i);
  row(p, 'lab cost', d.lab_cost);
  row(p, 'resource profile', d.resource_profile_id);
  const q = document.getElementById('queue'); q.replaceChildren();
  row(q, 'queued', d.queue_counts.queued);
  row(q, 'running', d.queue_counts.running);
  row(q, 'blocked', d.queue_counts.blocked);
  document.getElementById('stamp').textContent = new Date().toISOString();
}
async function poll() {
  try {
    const r = await fetch('/v1/dashboard', {cache: 'no-store'});
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
    const h = document.getElementById('health');
    h.textContent = 'healthy'; h.className = 'ok';
    const lr = await fetch('/v1/ledger?limit=20', {cache: 'no-store'});
    if (lr.ok) {
      const page = await lr.json();
      const body = document.getElementById('ledger'); body.replaceChildren();
      for (const e of page.events.slice().reverse()) {
        const tr = document.createElement('tr');
        for (const v of [e.ledger_seq, e.event_type, e.actor_principal, e.recorded_at_ns, e.event_hash.slice(0, 12) + '...']) {
          const td = document.createElement('td'); td.textContent = v; tr.append(td);
        }
        body.append(tr);
      }
    }
  } catch (e) {
    const h = document.getElementById('health');
    h.textContent = 'unreachable'; h.className = 'bad';
  }
}
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
`
