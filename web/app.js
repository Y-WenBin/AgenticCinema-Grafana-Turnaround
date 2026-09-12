// The playground's behaviour. Extracted from index.html so the Content-Security-
// Policy in agent/serve.py can refuse inline script outright -- `script-src
// 'self'` with no 'unsafe-inline' is a real second wall behind esc(), and an
// inline <script> would have forced either 'unsafe-inline' (no wall at all) or a
// per-response nonce (a template engine, for one page).

// Four questions, each verified cold against the live stack. Each one exercises
// a different claim, so the label says which -- someone arriving with a minute to
// spare should not have to guess what to type, or why it is worth typing.
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
// If the stack is unreachable the panel hides itself: a page that is honestly
// shorter beats one with a broken box in it.
function renderBackend(b) {
  const stats = b.tiles.filter(t => t.kind !== "bars");
  const bars  = b.tiles.filter(t => t.kind === "bars");

  $("tiles").innerHTML = stats.map(t => {
    // A zero that is *supposed* to be zero is the best number on this page --
    // no privacy breaches, no pools past 55h -- so it reads green rather than
    // as an empty figure. One Grafana could not answer reads dim instead.
    const good = t.value === 0 && !t.error;
    const cls = t.error ? "dead" : (good ? "good" : "");
    return '<div class="fig ' + cls + '"><div class="v">' + esc(t.display)
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

  // The hero states its claim twice -- in English on the left of the glass, and
  // here as the PromQL that produced it. Both halves come from this payload
  // rather than a literal in the HTML, because the show re-seeds every 15
  // minutes and a number baked into the page would start lying within the hour.
  // No bars tile (or an unreachable stack) simply leaves the hero half empty.
  const lead = bars.find(t => (t.bars || []).length);
  if (lead) {
    const top = lead.bars[0];
    $("heroQuery").textContent = lead.query;
    $("heroValue").textContent = top.display;
    $("heroCaption").textContent =
      "burned on " + top.label + " for a version that was superseded before review.";
    $("heroClaim").style.display = "";
  }

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
    // A run is usually 25-45s and is almost entirely model latency, which moves
    // around during the day -- measured on one unchanged revision: 23s at its
    // fastest, 63s at its slowest. So past 50s the honest thing to say is "still
    // going", not a guess at a cause. Blaming a cold start would be wrong most
    // of the time: one instance is kept warm.
    $("statusText").textContent = s;
    $("statusNote").textContent = s > 50
      ? "Still going — nearly all of a run is model latency, and that moves around."
      : "The three analysts below are working in parallel, each writing its own query.";
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

// The three analysts run concurrently (agent/producer.py wires them into one
// ParallelAgent), and a flat ordered table is the one shape that cannot show
// that -- rows 1..16 read as a queue. So the timeline is drawn twice: as a map
// of lanes, which shows what overlapped, and underneath as the full ordered
// table, which shows every expression in full.
//
// The lane colours are not decoration. --edge runs orange -> purple -> blue,
// and the three analysts take those stops in that order, so the gradient along
// the top of every panel is also the key to this section.
const PARALLEL = [
  ["schedule_analyst", "sched", "The show's own calendar — iterations, notes, sign-offs."],
  ["farm_analyst",     "farm",  "The render wall — waste, failure rates, the log line behind them."],
  ["crunch_guardian",  "crew",  "Hours logged, never below a pool of three."],
];
const KNOWN_AFTER = {
  remediator: "Drafts a fix, and is refused permission to apply it.",
};

function laneHTML(agent, cls, does, calls) {
  const rows = calls.map(c =>
    '<div class="call"><span class="tool">'
    + (c.grafana_mcp ? '<span class="star">✱</span> ' : "")
    + esc(c.tool) + '</span><span class="ms">'
    + (c.duration_ms == null ? "—" : Math.round(c.duration_ms)) + "</span></div>").join("");
  return '<div class="lane ' + cls + '"><div class="who">' + esc(agent)
    + ' <span class="n">' + calls.length + (calls.length === 1 ? " call" : " calls")
    + "</span></div>" + (does ? '<p class="does">' + esc(does) + "</p>" : "") + rows + "</div>";
}

function renderLanes(calls) {
  const by = new Map();
  calls.forEach(c => {
    if (!by.has(c.agent)) by.set(c.agent, []);
    by.get(c.agent).push(c);
  });

  // The three that overlapped, in gradient order, and only if they actually ran.
  $("laneMap").innerHTML = PARALLEL
    .filter(([agent]) => by.has(agent))
    .map(([agent, cls, does]) => laneHTML(agent, cls, does, by.get(agent))).join("");

  // Everything else, in the order it first appears -- so an agent added to the
  // pipeline later still lands here rather than vanishing from the page.
  const named = new Set(PARALLEL.map(p => p[0]));
  const rest = [...by.keys()].filter(a => !named.has(a))
    .map(a => laneHTML(a, "after", KNOWN_AFTER[a] || "", by.get(a)));

  // Synthesis writes the answer and queries nothing, by design: it may only
  // restate what the analysts returned. Zero calls means zero rows, so it would
  // otherwise be the one agent invisible on its own timeline. Named explicitly,
  // and only while that stays true.
  if (!by.has("synthesis")) {
    rest.push('<div class="lane after"><div class="who">synthesis '
      + '<span class="n">0 calls</span></div><p class="does">Wrote the answer above. '
      + "It queried nothing — by design it may only restate what the analysts returned."
      + "</p></div>");
  }
  $("laneAfter").innerHTML = rest.join("");
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

    const calls = data.timeline || [];
    const scored = data.evaluation || [];
    $("answer").textContent = data.answer || "(no answer)";
    $("askedQ").textContent = question;
    $("runMeta").innerHTML = [
      calls.length + (calls.length === 1 ? " call" : " calls"),
      calls.filter(c => c.grafana_mcp).length + " into Grafana",
      scored.length
        ? scored.filter(r => r.label === "pass").length + " / " + scored.length + " checks"
        : null,
    ].filter(Boolean).map(esc).join('<i>|</i>');
    renderScores(scored);
    renderLanes(calls);
    renderTimeline(calls);
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
