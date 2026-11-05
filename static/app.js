// Pitch Lab front end: streams a research run, shows the agents at work, renders the cited report
// with its evidence, and draws a dashboard for the pitchers involved.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (v, d = 1) => (v == null ? "–" : (v * 100).toFixed(d) + "%");
const store = {
    get(k, fallback) {
        try {
            const v = localStorage.getItem(k);
            return v == null ? fallback : JSON.parse(v);
        } catch {
            return fallback;
        }
    },
    set(k, v) {
        try {
            localStorage.setItem(k, JSON.stringify(v));
            return true;
        } catch {
            return false;
        }
    },
};

const EXAMPLES = [
    { q: "Why did Paul Skenes give up more home runs in 2026 than in 2025? Is it real or luck?" },
    { q: "Did Gerrit Cole come back from Tommy John surgery at full strength in 2026?" },
    { q: "Was Tarik Skubal's 2026 as good as 2025 under the hood, or did luck change?" },
    { q: "Which pitchers lost the most fastball velocity from 2025 to 2026, and did their results follow?" },
];

const FOCUS_LABEL = {
    stuff: "Stuff & arsenal",
    results_luck: "Results vs. luck",
    health_workload: "Health & workload",
    history_context: "History & league",
    league_scan: "League scan",
};
const PITCH_NAME = {
    FF: "4-Seam", SI: "Sinker", FC: "Cutter", SL: "Slider", ST: "Sweeper", CU: "Curveball", KC: "Knuckle curve",
    CH: "Changeup", FS: "Splitter", SV: "Slurve", FO: "Forkball", KN: "Knuckleball", SC: "Screwball", CS: "Slow curve", EP: "Eephus",
};
const SEASON_COLOR = { 2024: "var(--s2024)", 2025: "var(--s2025)", 2026: "var(--s2026)" };
const MONTHS = { 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct" };

const state = {
    source: null,
    runId: null,
    question: "",
    answers: [],
    clarifyRounds: 0,
    started: 0,
    timer: null,
    evidence: {},
    researchers: {},
    rounds: new Set(),
    tokens: 0,
    pitchers: [],
    activePitcher: null,
    result: null,
    evFilter: "all",
    draft: "", // the Writer's text so far, shown until the checked report arrives
    draftNote: "",
    draftFrame: 0,
};

// --- Theme, panels, history ---

(function initTheme() {
    const saved = store.get("pitchlab.theme", null);
    if (saved) document.documentElement.dataset.theme = saved;
    $("btn-theme").addEventListener("click", () => {
        const dark = document.documentElement.dataset.theme
            ? document.documentElement.dataset.theme === "dark"
            : matchMedia("(prefers-color-scheme: dark)").matches;
        document.documentElement.dataset.theme = dark ? "light" : "dark";
        store.set("pitchlab.theme", document.documentElement.dataset.theme);
        if (state.activePitcher) loadPitcher(state.activePitcher); // charts re-read colors
    });
})();

$("btn-context").addEventListener("click", () => {
    const open = document.body.classList.toggle("ctx-open");
    $("btn-context").setAttribute("aria-pressed", open);
    if (open && state.runId) showContext(state.runId);
});
$("btn-history").addEventListener("click", () => {
    renderHistory();
    $("drawer").classList.add("open");
});
$("drawer-close").addEventListener("click", () => $("drawer").classList.remove("open"));

function renderHistory() {
    const items = store.get("pitchlab.history", []);
    $("history").innerHTML = items.length ? "" : '<div class="empty">Finished reports are kept in this browser.</div>';
    for (const h of items) {
        const b = document.createElement("button");
        b.className = "hist";
        b.innerHTML = `${esc(h.question)}<small>${esc(h.verdict || "")} · ${new Date(h.ts).toLocaleString()}</small>`;
        b.onclick = () => {
            $("drawer").classList.remove("open");
            showResult(h.result, { fromHistory: true });
        };
        $("history").appendChild(b);
    }
}

function saveHistory(result) {
    const items = store.get("pitchlab.history", []);
    const verdict = (result.markdown.match(/Verdict:\s*([^*·\n]+)/) || [])[1]?.trim();
    items.unshift({ ts: Date.now(), question: result.question || state.question, verdict, result });
    while (items.length > 8) items.pop();
    // Large runs may not fit: drop the oldest until they do
    while (items.length && !store.set("pitchlab.history", items)) items.pop();
}

// --- Composer ---

for (const ex of EXAMPLES) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.innerHTML = esc(ex.q);
    b.onclick = () => {
        $("question").value = ex.q;
        $("question").focus();
    };
    $("examples").appendChild(b);
}
$("question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("ask").requestSubmit();
});
$("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("question").value.trim();
    if (!q) return;
    state.question = q;
    state.answers = [];
    state.clarifyRounds = 0;
    startRun();
});
$("stop").addEventListener("click", () => {
    if (state.source) state.source.close();
    finish("stopped");
    log("", "Stopped. The server stops the agents when the stream closes.", "err");
});

// --- A run ---

function resetDashboard() {
    state.pitchers = [];
    state.activePitcher = null;
    $("dash-tabs").innerHTML = "";
    $("dash-body").innerHTML = '<div class="empty">Pitchers from your research appear here. Or look one up.</div>';
}

function resetRun() {
    resetDashboard();
    state.evidence = {};
    state.researchers = {};
    state.rounds = new Set();
    state.tokens = 0;
    state.result = null;
    state.draft = "";
    state.draftNote = "";
    $("rounds").innerHTML = "";
    $("log").innerHTML = "";
    $("brief").classList.add("hidden");
    $("error").classList.add("hidden");
    $("clarify").classList.add("hidden");
    $("report-wrap").classList.add("hidden");
    for (const s of document.querySelectorAll(".step")) s.className = "step";
    for (const id of ["st-researchers", "st-rounds", "st-queries", "st-tokens"]) $(id).textContent = "0";
    resetContext();
}

function startRun(force = false) {
    if (state.source) state.source.close();
    resetRun();
    $("status").classList.remove("hidden");
    $("board").classList.remove("hidden");
    $("status-q").textContent = state.question;
    $("run").disabled = true;
    $("stop").classList.remove("hidden");
    state.started = Date.now();
    clearInterval(state.timer);
    state.timer = setInterval(() => ($("st-time").textContent = Math.round((Date.now() - state.started) / 1000) + "s"), 500);
    $("status").scrollIntoView({ behavior: "smooth", block: "start" });

    const params = new URLSearchParams({ question: state.question, answers: JSON.stringify(state.answers), rounds: state.clarifyRounds, force });
    const source = new EventSource("/run?" + params);
    state.source = source;
    source.onmessage = (msg) => handle(JSON.parse(msg.data));
    source.onerror = () => {
        if (state.source !== source) return;
        showError("Lost the connection to the server.");
        finish("failed");
    };
}

function finish(label) {
    clearInterval(state.timer);
    $("run").disabled = false;
    $("stop").classList.add("hidden");
    if (state.source) state.source.close();
    state.source = null;
    if (label === "failed" || label === "stopped") for (const s of document.querySelectorAll(".step.active")) s.classList.remove("active");
}

function showError(message) {
    $("error").textContent = message;
    $("error").classList.remove("hidden");
}

// The Writer's text as it streams in. Not yet checked, so it's labelled as a draft.
function showDraft() {
    if (state.result) return; // the checked report already arrived
    $("report-wrap").classList.remove("hidden");
    $("verify-badge").textContent = "";
    $("verify-panel").classList.add("hidden");
    $("btn-copy").disabled = $("btn-download").disabled = true; // nothing final to copy yet
    const rep = $("report");
    const note = state.draftNote || "Draft · the Writer is still writing. Every number is checked before the final report.";
    rep.innerHTML = `<div class="draft-note"><span class="pulse"></span>${esc(note)}</div>` +
        (state.draft ? renderMarkdown(state.draft) : "");
    rep.querySelectorAll(".cite").forEach((c) => (c.onclick = () => openEvidence(c.dataset.q, true)));
}

function setPhase(phase) {
    const order = ["clarify", "research", "rounds", "write", "verify"];
    const i = order.indexOf(phase);
    document.querySelectorAll(".step").forEach((el, j) => {
        el.classList.toggle("done", j < i);
        el.classList.toggle("active", j === i);
    });
}

function log(agent, text, cls = "") {
    const div = document.createElement("div");
    if (cls) div.className = cls;
    div.innerHTML = (agent ? `<b>${esc(agent)}</b> ` : "") + esc(text);
    $("log").appendChild(div);
    $("log").scrollTop = $("log").scrollHeight;
}

function handle(e) {
    switch (e.type) {
        case "start":
            state.runId = e.run_id;
            break;
        case "phase":
            setPhase(e.phase);
            if (e.phase === "verify" && state.draft) {
                state.draftNote = "Draft · now checking every number against its query, and every claim against the evidence. The checked report replaces this.";
                showDraft();
            }
            break;
        case "draft_start": // the Writer started (or restarted after a failure)
            state.draft = "";
            state.draftNote = "";
            showDraft();
            break;
        case "draft_delta":
            state.draft += e.delta;
            if (!state.draftFrame) state.draftFrame = requestAnimationFrame(() => {
                state.draftFrame = 0;
                showDraft();
            });
            break;
        case "context":
            if (document.body.classList.contains("ctx-open")) showContext(state.runId);
            break;
        case "activity":
            log(e.agent, e.text);
            break;
        case "usage":
            state.tokens = e.input + e.output;
            $("st-tokens").textContent = state.tokens > 9999 ? Math.round(state.tokens / 1000) + "k" : state.tokens;
            break;
        case "brief":
            $("brief").innerHTML = `<b>Researching:</b> ${esc(e.question)}`;
            $("brief").classList.remove("hidden");
            break;
        case "pitcher":
            addPitcher(e.mlbam_id, e.name);
            break;
        case "wave":
            setPhase("rounds");
            addRound(e.wave, e.followup);
            break;
        case "researcher":
            upsertResearcher(e);
            break;
        case "query":
            state.evidence[e.id] = e;
            $("st-queries").textContent = Object.keys(state.evidence).length;
            addQueryChip(e);
            break;
        case "query_error":
            addQueryChip({ by: e.agent, error: e.error, purpose: e.purpose, failed: e.failed, max_failed: e.max_failed });
            log(e.agent, "SQL error (retrying): " + e.error.split("\n")[0], "warn");
            break;
        case "gap_review":
            addGapReview(e);
            break;
        case "gap_decision":
            decideGap(e);
            break;
        case "budget":
            log("Lead", `time budget reached at ${e.seconds}s: no more research rounds`, "err");
            break;
        case "verify":
            log("Citation check", `draft: ${e.numbers_matched}/${e.numbers_checked} numbers matched, ${e.issues} issue(s) → sent back to the Writer`);
            break;
        case "clarify":
            finish("clarify");
            setPhase("clarify");
            $("board").classList.add("hidden");
            renderClarify(e);
            break;
        case "done":
            finish("done");
            document.querySelectorAll(".step").forEach((el) => {
                el.classList.remove("active");
                el.classList.add("done");
            });
            showResult(e);
            saveHistory(e);
            break;
        case "error":
            showError(e.message);
            finish("failed");
            break;
    }
}

// --- Clarification ---

function renderClarify(e) {
    const box = $("clarify");
    box.classList.remove("hidden");
    const scope = e.status !== "clarify"; // out of scope or off topic: nothing to research as asked
    box.innerHTML = `
        <div class="eyebrow">${scope ? "Outside what the data can answer" : "Before the team starts"}</div>
        <h3>${scope ? "We can't answer that one as asked" : "A few details first"}</h3>
        <p class="msg">${esc(e.message)}</p>`;
    const picks = [];
    (e.questions || []).forEach((q, qi) => {
        const div = document.createElement("div");
        div.className = "cq";
        div.innerHTML = `<p>${esc(q.question)}</p>`;
        const opts = document.createElement("div");
        opts.className = "opts";
        for (const o of q.options || []) {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "chip";
            b.textContent = o;
            b.onclick = () => {
                if (scope) {
                    // A suggested question, kept as an answer so the report can still say what wasn't answerable
                    state.answers = [...state.answers, { question: "Research this instead", answer: o }];
                    state.clarifyRounds += 1;
                    startRun();
                    return;
                }
                opts.querySelectorAll(".chip").forEach((c) => c.classList.remove("selected"));
                b.classList.add("selected");
                picks[qi] = o;
                other.value = "";
            };
            opts.appendChild(b);
        }
        const other = document.createElement("input");
        other.className = "other";
        other.placeholder = scope ? "Or type a new question…" : "Other…";
        other.oninput = () => {
            opts.querySelectorAll(".chip").forEach((c) => c.classList.remove("selected"));
            picks[qi] = other.value.trim() || undefined;
        };
        if (!scope) opts.appendChild(other);
        div.appendChild(opts);
        box.appendChild(div);
    });
    const actions = document.createElement("div");
    actions.className = "actions";
    if (!scope) {
        const go = document.createElement("button");
        go.className = "btn";
        go.type = "button";
        go.textContent = "Continue research";
        go.onclick = () => {
            const answered = (e.questions || []).map((q, i) => ({ question: q.question, answer: picks[i] })).filter((a) => a.answer);
            if (!answered.length) {
                go.textContent = "Pick an answer first";
                setTimeout(() => (go.textContent = "Continue research"), 1400);
                return;
            }
            state.answers = [...state.answers, ...answered];
            state.clarifyRounds += 1;
            startRun();
        };
        actions.appendChild(go);
    }
    if (!scope) {
        const anyway = document.createElement("button");
        anyway.className = "btn secondary";
        anyway.type = "button";
        anyway.textContent = "Research it as asked";
        anyway.onclick = () => startRun(true);
        actions.appendChild(anyway);
    }
    const edit = document.createElement("button");
    edit.className = "btn ghost";
    edit.type = "button";
    edit.textContent = "Edit my question";
    edit.onclick = () => {
        $("question").focus();
        window.scrollTo({ top: 0, behavior: "smooth" });
    };
    actions.append(edit);
    box.appendChild(actions);
    box.scrollIntoView({ behavior: "smooth", block: "center" });
}

// --- Research board ---

function addRound(n, followup) {
    if (state.rounds.has(n)) return;
    state.rounds.add(n);
    $("st-rounds").textContent = state.rounds.size;
    const div = document.createElement("div");
    div.className = "round";
    div.id = "round-" + n;
    div.innerHTML = `<div class="round-head"><span class="pill ${followup ? "followup" : ""}">Round ${n}</span>
        <small>${followup ? "The Lead went back for what the findings left open" : "Fan-out: researchers start together, in parallel"}</small>
        <span class="line"></span></div><div class="lanes"></div>`;
    $("rounds").appendChild(div);
}

function upsertResearcher(e) {
    addRound(e.wave, e.wave > 1);
    let card = $("r-" + e.id);
    if (!card) {
        card = document.createElement("div");
        card.className = "rcard";
        card.id = "r-" + e.id;
        card.style.setProperty("--fc", `var(--f-${e.focus}, var(--ink))`);
        card.innerHTML = `<div class="top"><span class="focus">${esc(FOCUS_LABEL[e.focus] || e.focus)}</span><span class="who">${esc(e.name)}</span></div>
            <div class="task">${esc(e.task)}</div>${e.why ? `<div class="why ${e.wave > 1 ? "" : "goal"}">${esc(e.why)}</div>` : ""}
            <div class="qs"></div><div class="state"><span class="spinner"></span> querying…</div>`;
        document.querySelector(`#round-${e.wave} .lanes`).appendChild(card);
        state.researchers[e.name] = card;
        $("st-researchers").textContent = Object.keys(state.researchers).length;
    }
    if (e.status !== "running") {
        const st = card.querySelector(".state");
        if (e.status === "done") {
            // SQL errors along the way are normal: the researcher saw the error and retried
            const errs = e.failed_queries ? ` · ${e.failed_queries} SQL error${e.failed_queries > 1 ? "s" : ""}, retried` : "";
            const late = e.salvaged ? " · wrapped up at its turn limit" : "";
            st.innerHTML = `✓ done in ${e.seconds}s · ${e.queries} quer${e.queries === 1 ? "y" : "ies"}${errs}${late}`;
            if (e.salvaged) st.title = "It used all its turns; one last call with no tools wrote the findings from the queries it had already run.";
        } else {
            // The researcher itself didn't finish: no findings from it, the run goes on
            card.classList.add("failed");
            st.innerHTML = `✕ didn't finish after ${e.seconds}s`;
            const why = document.createElement("div");
            why.className = "fail-note";
            why.innerHTML = `<b>No findings from this researcher.</b> It ${esc(e.reason || "stopped with an error")}.
                The Lead sees this as a gap; the rest of the run continues.`;
            card.appendChild(why);
        }
        const gaps = (e.gaps || "").trim();
        if (gaps && !/^none\.?$/i.test(gaps)) {
            const g = document.createElement("div");
            g.className = "gaps";
            g.textContent = "Gaps: " + gaps;
            card.appendChild(g);
        }
        const d = document.createElement("details");
        d.innerHTML = `<summary>Findings</summary><div class="findings"></div>`;
        d.querySelector(".findings").innerHTML = renderInline(esc((e.findings || "").replace(/\nGAPS:[\s\S]*$/, "")));
        card.appendChild(d);
    }
}

function addGapReview(e) {
    const box = document.createElement("div");
    box.className = "gap-review";
    box.id = "gap-" + e.round;
    box.innerHTML = `<div class="gr-head"><span class="gr-icon">↺</span><b>Gap review after round ${e.round}</b><span class="gr-state"><span class="spinner"></span> Lead deciding…</span></div>
        <ul>${e.gaps.map((g) => `<li><b>${esc(g.name)}:</b> ${esc(g.gap)}</li>`).join("")}</ul>`;
    $("rounds").appendChild(box);
}

function decideGap(e) {
    const st = document.querySelector(`#gap-${e.round} .gr-state`);
    if (!st) return;
    st.className = "gr-state " + (e.followup ? "yes" : "no");
    st.textContent = e.followup ? "→ follow-up round sent" : "→ no follow-up" + (e.reason ? ": " + e.reason : "");
}

function addQueryChip(e) {
    const card = state.researchers[e.by];
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "qchip" + (e.error ? " sqlerr" : "");
    chip.textContent = e.error ? "⚠ SQL error" : e.id;
    chip.title = e.error ? "The researcher's SQL failed; it got the error and a hint, and tries again. Click for details." : e.purpose;
    if (!e.error) chip.onclick = () => openEvidence(e.id, true);
    else if (card) {
        // Click: show what went wrong and the hint the researcher got
        chip.dataset.err = "e" + Math.random().toString(36).slice(2, 8);
        chip.onclick = () => {
            const open = card.querySelector(".sql-note")?.dataset.for === chip.dataset.err;
            card.querySelectorAll(".sql-note").forEach((n) => n.remove());
            if (open) return;
            const note = document.createElement("div");
            note.className = "sql-note";
            note.dataset.for = chip.dataset.err;
            const [msg, hint] = e.error.split("\nHint: ");
            note.innerHTML = `<b>${esc(e.purpose)}</b><div>${esc(msg)}</div>${hint ? `<div class="hint">Hint sent back: ${esc(hint)}</div>` : ""}
                <div class="muted">Doesn't count against its query budget${e.failed ? ` · failure ${e.failed} of ${e.max_failed} allowed` : ""}</div>`;
            card.querySelector(".qs").after(note);
        };
    }
    if (card) card.querySelector(".qs").appendChild(chip);
    else if (!e.error) log(e.by, `[${e.id}] ${e.purpose}`);
}

// --- Markdown (the subset the Writer uses), with citation chips ---

function renderInline(s) {
    // s is already escaped
    return s
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
        .replace(/\[(Q\d+(?:\s*[,;]\s*Q\d+)*)\]/g, (_, ids) => {
            const chips = ids.split(/\s*[,;]\s*/).map((q) => `<button type="button" class="cite" data-q="${q}">${q}</button>`).join("");
            return `<span class="cites">${chips}</span>`;
        });
}

function renderMarkdown(md) {
    const lines = md.replace(/\r/g, "").split("\n");
    let html = "";
    let i = 0;
    let section = "";
    while (i < lines.length) {
        const line = lines[i];
        if (!line.trim()) {
            i++;
            continue;
        }
        let m;
        if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
            const level = Math.min(3, Math.max(2, m[1].length));
            section = m[2];
            html += `<h${level}>${renderInline(esc(m[2]))}</h${level}>`;
            i++;
        } else if (line.startsWith(">")) {
            const block = [];
            while (i < lines.length && lines[i].startsWith(">")) block.push(lines[i++].replace(/^>\s?/, ""));
            const text = block.join(" ");
            const v = text.match(/Verdict:\s*([^*·|]+?)\s*\**\s*[·|]\s*Confidence:\s*\**\s*(\w+)/i);
            if (v) {
                const overlap = text.match(/Injury overlap:\s*\**\s*(\w+)/i); // older saved reports don't have it
                html += `<div class="verdict"><svg class="ball" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="14" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9 5c4 6 4 16 0 22M23 5c-4 6-4 16 0 22" fill="none" stroke="var(--accent)" stroke-width="2" stroke-dasharray="2 2"/></svg>
                    <div><div class="eyebrow" style="color:inherit;opacity:.7">Verdict</div><div class="label">${esc(v[1].replace(/\*+/g, "").trim())}</div></div>
                    <span class="conf">Confidence · ${esc(v[2])}</span>${overlap ? `<span class="conf">Injury overlap · ${esc(overlap[1])}</span>` : ""}</div>`;
                const rest = text.replace(/^.*?Confidence:\s*\**\s*\w+\**/i, "").replace(/^\s*\**\s*[·|]?\s*\**\s*Injury overlap:\s*\**\s*\w+\**/i, "").trim();
                if (rest) html += `<p>${renderInline(esc(rest))}</p>`;
            } else html += `<blockquote><p>${renderInline(esc(text))}</p></blockquote>`;
        } else if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
            const ordered = /^\s*\d+\./.test(line);
            const items = [];
            while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, ""));
            if (/next questions?/i.test(section)) {
                html += `<ul class="next-q">${items.map((t) => `<li><button type="button" data-ask="${esc(t.replace(/\*\*/g, ""))}">${renderInline(esc(t))}</button></li>`).join("")}</ul>`;
            } else {
                const tag = ordered ? "ol" : "ul";
                html += `<${tag}>${items.map((t) => `<li>${renderInline(esc(t))}</li>`).join("")}</${tag}>`;
            }
        } else if (line.trim().startsWith("|")) {
            const rows = [];
            while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
            const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
            const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
            html += "<table>" + body.map((r, ri) => "<tr>" + cells(r).map((c) => (ri === 0 ? `<th>${renderInline(esc(c))}</th>` : `<td>${renderInline(esc(c))}</td>`)).join("") + "</tr>").join("") + "</table>";
        } else {
            const para = [];
            while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|>|\s*([-*+]|\d+\.)\s|\s*\|)/.test(lines[i])) para.push(lines[i++]);
            html += `<p>${renderInline(esc(para.join(" ")))}</p>`;
        }
    }
    return html;
}

// Mark numbers the citation check couldn't match, so a reader knows which claims to double-check
function markUnverified(root, unmatched) {
    const norm = (s) => s.replace(/\*\*|\[Q[\d,\sQ;]+\]/g, "").replace(/\s+/g, " ").trim();
    for (const u of unmatched || []) {
        const snippet = norm(u.sentence).slice(0, 36);
        for (const block of root.querySelectorAll("p, li, td")) {
            if (!norm(block.textContent).includes(snippet)) continue;
            const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const at = node.data.indexOf(u.number);
                if (at < 0 || node.parentElement.closest("mark, .cite")) continue;
                const range = document.createRange();
                range.setStart(node, at);
                range.setEnd(node, at + u.number.length);
                const mark = document.createElement("mark");
                mark.className = "unverified";
                mark.title = `Not found in the rows of ${u.cited.join(", ")}`;
                range.surroundContents(mark);
                break;
            }
            break;
        }
    }
}

// --- Report + evidence ---

function showResult(r, { fromHistory = false } = {}) {
    state.result = r;
    state.evidence = r.evidence || {};
    if (fromHistory) {
        resetRun();
        state.result = r;
        state.evidence = r.evidence || {};
        $("status").classList.add("hidden");
        $("board").classList.add("hidden");
        state.question = r.question;
    }
    $("report-wrap").classList.remove("hidden");
    $("btn-copy").disabled = $("btn-download").disabled = false;
    const rep = $("report");
    rep.innerHTML = renderMarkdown(r.markdown);
    markUnverified(rep, r.verification?.unmatched);
    const known = new Set(Object.keys(state.evidence));
    rep.querySelectorAll(".cite").forEach((c) => {
        if (!known.has(c.dataset.q)) {
            c.classList.add("bad");
            c.title = "Not a query that ran";
        } else c.title = state.evidence[c.dataset.q].purpose;
        c.onclick = () => openEvidence(c.dataset.q, true);
    });
    rep.querySelectorAll("[data-ask]").forEach((b) => {
        b.onclick = () => {
            $("question").value = b.dataset.ask;
            state.question = b.dataset.ask;
            state.answers = [];
            state.clarifyRounds = 0;
            startRun();
        };
    });
    renderVerification(r.verification, r.stats);
    renderEvidence();
    if (fromHistory) resetDashboard();
    for (const p of r.pitchers || []) addPitcher(p.mlbam_id, p.name);
    if (r.stats) {
        $("st-time").textContent = Math.round(r.stats.seconds) + "s";
        $("st-rounds").textContent = r.stats.rounds;
        $("st-researchers").textContent = r.stats.researchers;
        $("st-queries").textContent = r.stats.queries;
    }
    $("report-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderVerification(v, stats) {
    const badge = $("verify-badge");
    const panel = $("verify-panel");
    panel.classList.add("hidden");
    if (!v) {
        badge.textContent = "";
        return;
    }
    const ok = v.issues === 0;
    badge.className = "verify-badge " + (ok ? "ok" : "partial");
    const icon = ok
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="m5 12 5 5 9-10"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/></svg>';
    badge.innerHTML = `${icon} ${v.numbers_matched}/${v.numbers_checked} numbers match their cited queries · ${v.citations.length} sources`;
    const items = [];
    if (v.revised) items.push(`<li>The draft had ${v.draft_issues} issue(s); the Writer revised it once against the query rows.</li>`);
    for (const u of v.unmatched) items.push(`<li><b>${esc(u.number)}</b> not found in ${esc(u.cited.join(", "))}: “${esc(u.sentence.slice(0, 140))}…”</li>`);
    for (const u of v.uncited) items.push(`<li>No citation for <b>${esc(u.numbers.join(", "))}</b>: “${esc(u.sentence.slice(0, 140))}…”</li>`);
    for (const q of v.unknown_ids) items.push(`<li><b>${esc(q)}</b> is cited but never ran.</li>`);
    panel.innerHTML = `<b>How the citations were checked.</b> For every sentence, code looked up each number in the rows of the queries it cites
        (as is, as a percentage, or as a change between two cited values), at the precision the sentence uses.
        ${stats ? `${stats.researchers} researchers ran ${stats.queries} queries over ${stats.rounds} round(s) in ${Math.round(stats.seconds)}s.` : ""}
        ${stats?.failed_queries ? `${stats.failed_queries} SQL error(s) were sent back to the researchers and retried; failed queries have no Q id, so the report can't cite them.` : ""}
        ${stats?.failed_researchers ? `<b>${stats.failed_researchers} researcher(s) didn't finish</b>; their tasks show up as gaps.` : ""}
        ${items.length ? `<ul>${items.join("")}</ul>` : "<ul><li>Every number matched.</li></ul>"}`;
    badge.onclick = badge.onkeydown = (ev) => {
        if (ev.type === "keydown" && ev.key !== "Enter") return;
        panel.classList.toggle("hidden");
    };
}

function renderEvidence() {
    const list = $("ev-list");
    const cited = new Set(state.result?.verification?.citations || []);
    const all = Object.values(state.evidence).sort((a, b) => +a.id.slice(1) - +b.id.slice(1));
    const filters = [
        ["all", `All ${all.length}`],
        ["cited", `Cited ${cited.size}`],
        ["statcast", "Statcast"],
        ["bref", "Baseball Ref"],
        ["mlb_api", "MLB API"],
    ];
    $("ev-filters").innerHTML = "";
    for (const [k, label] of filters) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chip" + (state.evFilter === k ? " selected" : "");
        b.textContent = label;
        b.onclick = () => {
            state.evFilter = k;
            renderEvidence();
        };
        $("ev-filters").appendChild(b);
    }
    $("ev-count").textContent = `· ${cited.size} cited`;
    list.innerHTML = "";
    const show = all.filter((ev) =>
        state.evFilter === "all" ? true : state.evFilter === "cited" ? cited.has(ev.id) : ev.sources.some((s) => s.key === state.evFilter),
    );
    for (const ev of show) {
        const item = document.createElement("div");
        item.className = "ev";
        item.id = "ev-" + ev.id;
        const head = document.createElement("button");
        head.innerHTML = `<span class="qid">${ev.id}</span><span class="purpose">${esc(ev.purpose)}</span><span class="meta">${ev.rows.length} rows</span>`;
        head.onclick = () => item.classList.toggle("open");
        const body = document.createElement("div");
        body.className = "body";
        const src = ev.sources.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">↗ ${esc(s.label)}</a>`).join("");
        const cols = ev.columns.map((c) => `<th>${esc(c)}</th>`).join("");
        const rows = ev.rows.map((r) => "<tr>" + r.map((v) => `<td title="${esc(v)}">${esc(v ?? "")}</td>`).join("") + "</tr>").join("");
        body.innerHTML = `<div class="by">Run by ${esc(ev.by)} · ${ev.elapsed_ms} ms · tables: ${esc(ev.tables.join(", "))}${ev.truncated ? " · first rows only" : ""}</div>
            <div class="src">${src}</div>
            <div class="tbl"><table><tr>${cols}</tr>${rows || '<tr><td>(no rows)</td></tr>'}</table></div>`;
        item.append(head, body);
        list.appendChild(item);
    }
}

function openEvidence(qid, scroll) {
    if (!state.evidence[qid]) return;
    if ($("report-wrap").classList.contains("hidden")) {
        // During a run there is no report yet: show the evidence panel on its own
        $("report-wrap").classList.remove("hidden");
        $("report").innerHTML = '<div class="empty">The report appears here when the Writer is done. Evidence so far is on the right.</div>';
        $("verify-badge").textContent = "";
    }
    if (!$("ev-" + qid)) {
        state.evFilter = "all";
        renderEvidence();
    }
    const el = $("ev-" + qid);
    el.classList.add("open", "flash");
    setTimeout(() => el.classList.remove("flash"), 1400);
    document.querySelectorAll(".cite.active").forEach((c) => c.classList.remove("active"));
    document.querySelectorAll(`.cite[data-q="${qid}"]`).forEach((c) => c.classList.add("active"));
    if (scroll) {
        const list = $("ev-list");
        list.scrollTo({ top: el.offsetTop - list.offsetTop - 8, behavior: "smooth" });
        if (window.innerWidth < 1000) el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
}

function exportMarkdown() {
    const r = state.result;
    if (!r) return "";
    const cited = r.verification?.citations || [];
    let md = r.markdown + "\n\n## Sources\n\n";
    for (const q of cited) {
        const ev = r.evidence[q];
        if (!ev) continue;
        md += `- **[${q}]** ${ev.purpose} — ${ev.sources.map((s) => `${s.label} (${s.url})`).join("; ")}\n`;
    }
    return md;
}
$("btn-download").onclick = () => {
    const blob = new Blob([exportMarkdown()], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "pitch-lab-report.md";
    a.click();
    URL.revokeObjectURL(a.href);
};
$("btn-copy").onclick = async () => {
    try {
        await navigator.clipboard.writeText(exportMarkdown());
        $("btn-copy").textContent = "Copied";
    } catch {
        $("btn-copy").textContent = "Copy failed";
    }
    setTimeout(() => ($("btn-copy").textContent = "Copy"), 1400);
};

// --- Pitcher dashboard ---

function addPitcher(id, name) {
    if (!id || state.pitchers.some((p) => p.id === id)) return;
    state.pitchers.push({ id, name });
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.dataset.id = id;
    b.textContent = name;
    b.onclick = () => loadPitcher(id);
    $("dash-tabs").appendChild(b);
    if (!state.activePitcher) loadPitcher(id);
}

async function loadPitcher(id) {
    state.activePitcher = id;
    document.querySelectorAll("#dash-tabs .chip").forEach((c) => c.classList.toggle("selected", +c.dataset.id === id));
    const body = $("dash-body");
    body.innerHTML = '<div class="empty"><span class="spinner" style="display:inline-block"></span></div>';
    let d;
    try {
        const res = await fetch("/api/pitcher/" + id);
        if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
        d = await res.json();
    } catch (err) {
        if (state.activePitcher === id) body.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
        return;
    }
    if (state.activePitcher === id) renderDashboard(body, d); // the page may have moved on while this loaded
}

function renderDashboard(body, d) {
    const seasons = d.seasons;
    const name = d.profile.full_name || seasons[0]?.name || "Pitcher";
    const initials = name.split(" ").map((w) => w[0]).slice(0, 2).join("");
    const age = d.profile.birth_date ? Math.floor((new Date("2026-07-01") - new Date(d.profile.birth_date)) / 3.156e10) : null;
    body.innerHTML = `
        <div class="player">
            <div class="avatar">${esc(initials)}</div>
            <div><h3>${esc(name)}</h3>
                <div class="sub">${[d.profile.pitch_hand ? d.profile.pitch_hand + "HP" : "", d.team, age ? `age ${age} in 2026` : "", d.profile.mlb_debut_date ? "debut " + d.profile.mlb_debut_date : "", "MLBAM " + d.mlbam_id].filter(Boolean).map(esc).join(" · ")}</div></div>
            <div class="links"><a class="icon-btn" href="${d.links.savant}" target="_blank" rel="noopener">↗ Savant</a><a class="icon-btn" href="${d.links.mlb}" target="_blank" rel="noopener">↗ MLB.com</a>
            <button class="icon-btn" id="ask-about" type="button">Ask about ${esc(name.split(" ").slice(-1)[0])}</button></div>
        </div>
        <div class="tiles" id="tiles"></div>
        <div class="charts" id="charts"></div>`;
    $("ask-about").onclick = () => {
        $("question").value = `Why did ${name}'s results change from ${seasons.length > 1 ? seasons[seasons.length - 2].season : 2025} to ${seasons[seasons.length - 1].season}? Real skill, luck, or injury?`;
        window.scrollTo({ top: 0, behavior: "smooth" });
        $("question").focus();
    };

    // Stat tiles: the latest season, with the change from the season before (arrow + words, not color alone)
    const cur = seasons[seasons.length - 1];
    const prev = seasons.length > 1 ? seasons[seasons.length - 2] : null;
    const tiles = [
        ["K%", "k_pct", (v) => pct(v), true, 100, "pts"],
        ["BB%", "bb_pct", (v) => pct(v), false, 100, "pts"],
        ["Whiff%", "whiff_pct", (v) => pct(v), true, 100, "pts"],
        ["FB velo", "avg_velo_fastball", (v) => (v == null ? "–" : v.toFixed(1)), true, 1, "mph"],
        ["xwOBA", "xwoba", (v) => (v == null ? "–" : v.toFixed(3).replace(/^0/, "")), false, 1000, "pts"],
        ["wOBA − xwOBA", "woba_minus_xwoba", (v) => (v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(3).replace(/^(-?)0/, "$1")), null, 1000, "pts"],
    ];
    for (const [label, key, f, higherBetter, scale, unit] of tiles) {
        const t = document.createElement("div");
        t.className = "tile";
        let delta = "";
        if (prev && cur[key] != null && prev[key] != null) {
            const dv = (cur[key] - prev[key]) * scale;
            const dir = Math.abs(dv) < (scale === 1 ? 0.15 : scale === 100 ? 0.4 : 4) ? "flat" : (dv > 0) === higherBetter ? "up" : "down";
            const arrow = dv > 0 ? "▲" : dv < 0 ? "▼" : "•";
            const word = higherBetter === null ? (dv > 0 ? "unluckier" : "luckier") : dir === "flat" ? "steady" : dir === "up" ? "better" : "worse";
            const shown = scale === 1 ? dv.toFixed(1) : Math.round(dv * 10) / 10;
            delta = `<span class="delta ${higherBetter === null ? "flat" : dir}">${arrow} ${Math.abs(shown)} ${unit} vs ${prev.season} · ${word}</span>`;
        }
        t.innerHTML = `<small>${label} · ${cur.season}</small><b>${f(cur[key])}</b>${delta}`;
        $("tiles").appendChild(t);
    }
    const sample = document.createElement("div");
    sample.className = "tile";
    sample.innerHTML = `<small>Pitches · ${cur.season}</small><b>${cur.pitches.toLocaleString()}</b><span class="delta flat">${cur.batters_faced} batters faced</span>`;
    $("tiles").appendChild(sample);

    const charts = $("charts");
    const seasonList = [...new Set(d.months.map((m) => m.season))];
    const monthTicks = Object.entries(MONTHS).map(([v, label]) => ({ v: +v, label }));
    lineChart(charts, {
        title: "Fastball velocity by month",
        note: "Mean release speed of 4-seamers, sinkers and cutters (mph)",
        series: seasonList.map((s) => ({
            name: String(s),
            color: SEASON_COLOR[s],
            points: d.months.filter((m) => m.season === s && m.avg_velo_fastball != null && m.pitches >= 30).map((m) => ({ x: m.game_month, y: m.avg_velo_fastball, extra: m.pitches + " pitches" })),
        })),
        xTicks: monthTicks,
        xLabel: (x) => MONTHS[x],
        fmt: (v) => v.toFixed(1),
    });
    lineChart(charts, {
        title: "xwOBA allowed by month",
        note: "Expected wOBA from contact quality, K and BB. Lower is better for the pitcher",
        series: seasonList.map((s) => ({
            name: String(s),
            color: SEASON_COLOR[s],
            points: d.months.filter((m) => m.season === s && m.xwoba != null && m.pitches >= 60).map((m) => ({ x: m.game_month, y: m.xwoba, extra: m.pitches + " pitches" })),
        })),
        xTicks: monthTicks,
        xLabel: (x) => MONTHS[x],
        fmt: (v) => v.toFixed(3).replace(/^0/, ""),
    });
    const totals = {};
    for (const a of d.arsenal) totals[a.pitch_type] = (totals[a.pitch_type] || 0) + a.pitches;
    const types = Object.keys(totals).sort((a, b) => totals[b] - totals[a]).slice(0, 7);
    const arsenalSeasons = [...new Set(d.arsenal.map((a) => a.season))];
    const find = (t, s) => d.arsenal.find((a) => a.pitch_type === t && a.season === s);
    barChart(charts, {
        title: "Pitch mix by season",
        note: "Share of all pitches (%). Hover for velocity and whiff rate",
        categories: types,
        groups: arsenalSeasons.map((s) => ({ name: String(s), color: SEASON_COLOR[s], season: s })),
        value: (t, g) => (find(t, g.season) ? find(t, g.season).usage_pct * 100 : null),
        fmt: (v) => v.toFixed(0) + "%",
        detail: (t, g) => {
            const a = find(t, g.season);
            return a ? `<div>${PITCH_NAME[t] || t} · ${a.pitches} pitches</div><div>${a.avg_velo?.toFixed(1)} mph · whiff ${pct(a.whiff_pct)}</div>` : "";
        },
    });
    const career = d.career.filter((c) => c.ip >= 10);
    lineChart(charts, {
        title: "ERA vs FIP by season",
        note: career.length ? "Baseball Reference, seasons with 10+ IP. ERA above FIP often means bad luck or defense" : "No Baseball Reference seasons with 10+ IP",
        series: [
            { name: "ERA", color: "var(--s2024)", points: career.map((c) => ({ x: c.season, y: c.ERA, extra: c.ip + " IP" })) },
            { name: "FIP", color: "var(--s2025)", points: career.map((c) => ({ x: c.season, y: c.FIP, extra: c.WAR != null ? "WAR " + c.WAR : "" })) },
        ],
        xLabel: (x) => "’" + String(x).slice(2),
        fmt: (v) => v.toFixed(2),
    });
    const il = document.createElement("div");
    il.className = "chart";
    il.style.gridColumn = "1 / -1";
    il.innerHTML = `<h4>Injured list, 2024–2026</h4><div class="note">MLB Stats API transactions, parsed into stints</div>`;
    const ilList = document.createElement("div");
    ilList.className = "il-list";
    if (!d.il.length) ilList.innerHTML = '<div class="empty">No injured list stints 2024–2026.</div>';
    for (const s of d.il) {
        const row = document.createElement("div");
        row.className = "il-item";
        row.innerHTML = `<b>${esc(s.il_type)}</b><span>${esc(s.start_date)} → ${esc(s.end_date || "open")}${s.days_on_il != null ? ` · ${s.days_on_il} days` : ""} — ${esc(s.injury_note || s.placement_description)}</span>`;
        ilList.appendChild(row);
    }
    il.appendChild(ilList);
    charts.appendChild(il);
}

// Search any pitcher
let searchTimer = null;
$("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = $("search").value.trim();
    if (q.length < 2) return $("search-results").classList.add("hidden");
    searchTimer = setTimeout(async () => {
        const res = await fetch("/api/search?q=" + encodeURIComponent(q));
        const items = (await res.json()).filter((p) => p.pitches);
        const ul = $("search-results");
        ul.innerHTML = items.length ? "" : '<li><button type="button" disabled>No pitcher with Statcast data matches</button></li>';
        for (const p of items) {
            const li = document.createElement("li");
            li.innerHTML = `<button type="button">${esc(p.name)} <small>${esc([p.throws ? p.throws + "HP" : "", p.last_team, p.statcast_seasons].filter(Boolean).join(" · "))}</small></button>`;
            li.querySelector("button").onclick = () => {
                ul.classList.add("hidden");
                $("search").value = "";
                addPitcher(p.mlbam_id, p.name);
                loadPitcher(p.mlbam_id);
            };
            ul.appendChild(li);
        }
        ul.classList.remove("hidden");
    }, 180);
});
document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) $("search-results").classList.add("hidden");
});
