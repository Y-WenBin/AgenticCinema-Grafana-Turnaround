// The playground's behaviour. Extracted from index.html so the Content-Security-
// Policy in agent/serve.py can refuse inline script outright -- `script-src
// 'self'` with no 'unsafe-inline' is a real second wall behind esc(), and an
// inline <script> would have forced either 'unsafe-inline' (no wall at all) or a
// per-response nonce (a template engine, for one page).

// The four questions verified cold against the live stack (docs/DEMO.md). Each
// is here to prove a different claim, so the label says which -- a judge with
// sixty seconds should not have to guess what to type or why.
const SAMPLES = [
  ["Why is SEQ0420 slipping, and what is it costing in artist-days?", "the join, priced"],
  ["Who is heading for crunch, and when?",                            "the privacy floor, live"],
  ["What do I change to avoid both?",                                 "the proposed fix"],
  ["Is any vendor behaving unlike the others?",                       "the outlier detector"],
];

const $ = (id) => document.getElementById(id);
// Single quotes are escaped too. Every interpolation today sits in a
// double-quoted attribute or element text, so `'` is currently harmless -- but
// the day someone writes class='x' this is the difference between an escape and
// an XSS hole, and the cost of not having that day is one character.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

$("curl").textContent =
  "curl -s -H 'content-type: application/json' \\\n" +
  "  -d '{\"question\": \"" + SAMPLES[0][0] + "\"}' \\\n  " +
  location.origin + "/ask";

SAMPLES.forEach(([text, proves]) => {
  const b = document.createElement("button");
  b.className = "chip";
  b.innerHTML = "<b>" + esc(text) + "</b> &nbsp;<em>" + esc(proves) + "</em>";
  b.onclick = () => { $("q").value = text; $("q").focus(); };
  $("chips").appendChild(b);
});

async function capacity() {
  try {
    const c = await (await fetch("/api/capacity")).json();
    const left = Math.max(0, c.daily_budget - c.asks_today);
    $("cap").textContent = left + " of " + c.daily_budget + " runs left today"
      + (c.in_flight ? " · " + c.in_flight + " running now" : "");
    $("go").disabled = left === 0;
  } catch { /* capacity is a courtesy; never block asking on it */ }
}
capacity();

// The backend view. Every number here came out of Grafana Cloud a few seconds
// ago; the panel shows the exact query beside each one, because "we query
// Grafana" and "here is the PromQL that produced 52.7" are different claims.
// If the stack is unreachable the panel hides itself -- a judge should see a
// page that is honestly shorter, not one with a broken box in it.
function renderBackend(b) {
  const stats = b.tiles.filter(t => t.kind !== "bars");
  const bars  = b.tiles.filter(t => t.kind === "bars");

  $("tiles").innerHTML = stats.map(t => {
    // A zero that is *supposed* to be zero is the best number on this page --
    // no privacy breaches, no pools past 55h -- so it reads green rather than
    // as an empty tile. A tile Grafana could not answer reads dim instead.
    const good = t.value === 0 && !t.error;
    const cls = t.error ? "dead" : (good ? "good" : "");
    return '<div class="tile ' + cls + '"><div class="v">' + esc(t.display)
      + '</div><div class="t">' + esc(t.title) + '</div>'
      + '<div class="c">' + esc(t.caption) + "</div></div>";
  }).join("");

  $("bars").innerHTML = bars.map(t => {
    const rows = t.bars || [];
    if (!rows.length) return "";
    const top = rows[0].value || 1;
    return '<h2 style="margin:18px 0 2px">' + esc(t.title) + "</h2>"
      + rows.map(r =>
          '<div class="bar"><span class="k">' + esc(r.label) + "</span>"
          + '<span class="track"><span class="fill" style="width:'
          + Math.max(2, Math.round((r.value / top) * 100)) + '%"></span></span>'
          + '<span class="n">' + esc(r.display) + "</span></div>").join("");
  }).join("");

  $("qSummary").textContent =
    "Every query this panel is allowed to run (" + b.tiles.length + ") — show them";
  $("qBody").innerHTML = b.tiles.map(t =>
    "<tr><td>" + esc(t.title) + '</td><td><span class="src">'
    + esc(t.source === "prom" ? "PromQL" : "LogQL") + "</span></td>"
    + '<td class="q"><span class="expr">' + esc(t.query) + "</span></td></tr>").join("");

  $("backendNote").innerHTML =
    "Metrics read back over " + esc(b.lookback) + " (the show re-seeds every 15 min); "
    + "evaluation scores over " + esc(b.window) + ". Cached server-side for 30 s"
    + (b.stale ? " — <strong>showing the last good read; Grafana did not answer just now.</strong>"
               : ", so this is at most half a minute old.");
  $("backendPanel").style.display = "";
}

async function loadBackend() {
  try {
    const res = await fetch("/api/backend");
    if (!res.ok) return;                 // 503: leave the panel hidden
    const b = await res.json();
    if (b && b.tiles && b.tiles.length) renderBackend(b);
  } catch { /* the page works without it */ }
}
loadBackend();

function notice(html, bad) {
  $("err").innerHTML = '<div class="notice' + (bad ? " bad" : "") + '">' + html + "</div>";
}

// A run is long enough that a bare spinner reads as a hang. This reports the
// elapsed time, which is true, rather than faking stage progress the page
// cannot actually observe -- the real timeline arrives with the answer.
let timer = null;
function startClock() {
  const t0 = Date.now();
  $("status").classList.add("on");
  const tick = () => {
    const s = Math.round((Date.now() - t0) / 1000);
    // A typical run is 20-40s. Past that, the likeliest cause by far is a cold
    // start -- the service scales to zero, and the first request after an idle
    // period pays for the container and the ADK import graph.
    $("statusText").textContent = "Running the pipeline — " + s + "s"
      + (s > 45 ? " (a cold start adds a few seconds)" : "");
  };
  tick();
  timer = setInterval(tick, 1000);
}
function stopClock() { clearInterval(timer); $("status").classList.remove("on"); }

function renderScores(rows) {
  if (!rows || !rows.length) { $("scoresPanel").style.display = "none"; return; }
  $("scoresPanel").style.display = "";
  // actor_type is "deterministic" or "ai" (agent/evaluation.py). Anything not
  // explicitly deterministic was judged by a model, so count it that way rather
  // than matching a name -- a silent miscount here would misdescribe the tier.
  const isDet = (r) => r.actor_type === "deterministic";
  $("scores").innerHTML = rows.map(r =>
    '<span class="score" title="' + esc(r.explanation || "") + '">'
    + '<span class="dot ' + (r.label === "pass" ? "pass" : "fail") + '"></span>'
    + esc(r.name.replace(/_/g, " ")) + ' <span class="n">' + Number(r.score).toFixed(2)
    + '</span><span class="by">' + (isDet(r) ? "ground truth" : "judge") + "</span></span>"
  ).join("");
  const det = rows.filter(isDet).length;
  const llm = rows.length - det;
  $("scoresNote").textContent = det
    + " deterministic checks against ground truth, " + llm
    + " scored by a second Gemini call acting as judge. Both are emitted as "
    + "OpenTelemetry gen_ai.evaluation.result events into the same stack the "
    + "agents query — hover a check to see why it scored that way.";
}

function renderTimeline(calls) {
  const n = calls.length, g = calls.filter(c => c.grafana_mcp).length;
  $("tlSummary").textContent = n + " tool calls, " + g + " into Grafana Cloud — show them";
  $("tlBody").innerHTML = calls.map(c => {
    const a = c.args || {};
    const query = a.expr || a.query || a.q || a.text
      || Object.values(a).find(v => typeof v === "string" && v.length > 12) || "";
    return "<tr><td>" + c.seq + (c.grafana_mcp ? ' <span class="star">✱</span>' : "")
      + "</td><td>" + esc(c.agent) + "</td><td>" + esc(c.tool)
      + '</td><td class="q"><span class="expr">' + esc(query)
      + '</span></td><td class="ms">' + (c.duration_ms == null ? "—" : Math.round(c.duration_ms))
      + "</td></tr>";
  }).join("");
}

async function ask() {
  const question = $("q").value.trim() || $("q").placeholder;
  if (question.length < 3) return;
  $("go").disabled = true; $("err").innerHTML = ""; $("out").classList.remove("on");
  startClock();

  // Cloud Run's own ceiling is 300s; give up first so the page can say
  // something useful instead of the browser showing a bare network error.
  const abort = new AbortController();
  const bail = setTimeout(() => abort.abort(), 240000);

  try {
    const res = await fetch("/ask", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({question}),
      signal: abort.signal,
    });
    const data = await res.json();

    if (res.status === 429) {
      notice(esc(data.error) + (data.retry_after
        ? " <em>Try again in about " + Math.ceil(data.retry_after / 60) + " min.</em>" : ""));
      return;
    }
    if (data.error) { notice(esc(data.error), true); return; }
    if (data.halted_by) {
      notice("The run stopped early (<code>" + esc(data.halted_by) + "</code>): "
             + esc(data.halt_detail || ""), true);
    }

    $("answer").textContent = data.answer || "(no answer)";
    renderScores(data.evaluation);
    renderTimeline(data.timeline || []);
    $("out").classList.add("on");
    $("out").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) {
    notice(e.name === "AbortError"
      ? "That run passed four minutes and was given up on. The stack may be mid re-seed — worth one retry."
      : "Could not reach the agent: " + esc(e.message), true);
  } finally {
    clearTimeout(bail); stopClock(); $("go").disabled = false; capacity();
  }
}

$("go").onclick = ask;
$("q").addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") ask();
});
