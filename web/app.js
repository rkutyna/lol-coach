/* lol-coach viewer: dashboard, per-game charts, minimap replay, moment cards.
   Data comes from window.LOLCOACH (web/data.js), written by tools/build_review.py. */

const D = window.LOLCOACH;
const SVG_NS = "http://www.w3.org/2000/svg";

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const svgEl = (tag, attrs = {}) => {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
const clock = (min) => `${Math.floor(min)}:${String(Math.round((min % 1) * 60)).padStart(2, "0")}`;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* Dates arrive as UTC (`played_utc`). A game played at 19:00 local already
   falls on the NEXT UTC day, so anything rendered straight from that string
   is a day ahead for an evening game. Everything shown to the player is
   therefore converted to their own timezone first. */
const asDate = (iso) => {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
};
const pad2 = (n) => String(n).padStart(2, "0");
const localYMD = (iso) => {
  const d = asDate(iso);
  return d ? `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
           : String(iso).slice(0, 10);
};
const localMD = (iso) => {
  const d = asDate(iso);
  return d ? `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` : String(iso).slice(5, 10);
};
const localStamp = (iso) => {
  const d = asDate(iso);
  return d ? `${localYMD(iso)} ${pad2(d.getHours())}:${pad2(d.getMinutes())} local`
           : String(iso).replace("T", " ").slice(0, 16);
};
/* Whole calendar days between two local dates - not elapsed hours. A game at
   21:00 yesterday is "yesterday" even though it was 15 hours ago. */
const daysAgoLocal = (iso) => {
  const d = asDate(iso);
  if (!d) return null;
  const midnight = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  return Math.round((midnight(new Date()) - midnight(d)) / 86400000);
};

/* ---------- tiny markdown renderer (headings, lists, tables, inline) ---------- */

function inline(s) {
  return s
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*]+)\*/g, "$1<em>$2</em>")
    // Underscore emphasis, but never inside snake_case identifiers.
    .replace(/(^|[\s(])_([^_]+)_(?=[\s.,;:)]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1");
}

function markdown(src) {
  const out = [];
  const lines = src.split("\n");
  let list = null, para = [], table = null;

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
  };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushTable = () => {
    if (!table) return;
    const [head, ...rows] = table;
    out.push("<table><thead><tr>" + head.map((h) => `<th>${inline(h)}</th>`).join("") +
      "</tr></thead><tbody>" +
      rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
      "</tbody></table>");
    table = null;
  };
  const flushAll = () => { flushPara(); flushList(); flushTable(); };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const cells = line.match(/^\|(.+)\|$/);
    if (cells) {
      const parts = cells[1].split("|").map((c) => c.trim());
      if (parts.every((c) => /^:?-{2,}:?$/.test(c))) continue;  // separator row
      flushPara(); flushList();
      (table ||= []).push(parts);
      continue;
    }
    flushTable();

    if (!line) { flushPara(); flushList(); continue; }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flushAll(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    if (/^(-{3,}|_{3,})$/.test(line)) { flushAll(); out.push("<hr>"); continue; }

    const li = line.match(/^\s*([-*]|\d+\.)\s+(.*)$/);
    if (li) {
      flushPara();
      const want = /^\d/.test(li[1]) ? "ol" : "ul";
      if (list !== want) { flushList(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inline(li[2])}</li>`);
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushAll();
  return out.join("\n");
}

/* ---------- tooltip ---------- */

const tip = el("div", "tooltip hidden");
document.body.appendChild(tip);
const showTip = (html, x, y) => {
  tip.innerHTML = html;
  tip.classList.remove("hidden");
  const r = tip.getBoundingClientRect();
  tip.style.left = `${Math.min(x + 14, window.innerWidth - r.width - 8)}px`;
  tip.style.top = `${Math.max(8, y - r.height - 12)}px`;
};
const hideTip = () => tip.classList.add("hidden");

/* ---------- charts ---------- */

const PAD = { l: 46, r: 18, t: 12, b: 26 };

function axes(svg, w, h, xMax, yTicks, fmtY) {
  for (const { v, y } of yTicks) {
    svg.appendChild(svgEl("line", { class: "grid-line", x1: PAD.l, x2: w - PAD.r, y1: y, y2: y }));
    const t = svgEl("text", { class: "tick", x: PAD.l - 8, y: y + 4, "text-anchor": "end" });
    t.textContent = fmtY(v);
    svg.appendChild(t);
  }
  for (let m = 0; m <= xMax; m += 5) {
    const x = PAD.l + (m / xMax) * (w - PAD.l - PAD.r);
    const t = svgEl("text", { class: "tick", x, y: h - PAD.b + 16, "text-anchor": "middle" });
    t.textContent = `${m}m`;
    svg.appendChild(t);
  }
  svg.appendChild(svgEl("line", { class: "axis-line", x1: PAD.l, x2: w - PAD.r, y1: h - PAD.b, y2: h - PAD.b }));
}

/** Team gold lead over time: a diverging area around zero. */
function goldChart(game) {
  const { minutes, gold_delta } = game.tracks;
  const w = 960, h = 220;
  const xMax = Math.ceil(minutes[minutes.length - 1]);
  const peak = Math.max(2000, ...gold_delta.map(Math.abs));
  const X = (m) => PAD.l + (m / xMax) * (w - PAD.l - PAD.r);
  const Y = (g) => PAD.t + ((peak - g) / (2 * peak)) * (h - PAD.t - PAD.b);

  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, role: "img" });
  const yTicks = [peak, peak / 2, 0, -peak / 2, -peak].map((v) => ({ v, y: Y(v) }));
  axes(svg, w, h, xMax, yTicks, (v) => `${v > 0 ? "+" : ""}${Math.round(v / 1000)}k`);

  // Split the area at the zero line so ahead and behind read as opposites.
  const zero = Y(0);
  for (const [sign, color] of [[1, css("--ahead")], [-1, css("--behind")]]) {
    let d = "";
    minutes.forEach((m, i) => {
      const v = gold_delta[i];
      const y = sign > 0 ? Math.min(Y(v), zero) : Math.max(Y(v), zero);
      d += `${i ? "L" : "M"}${X(m)},${y}`;
    });
    d += `L${X(minutes[minutes.length - 1])},${zero}L${X(minutes[0])},${zero}Z`;
    svg.appendChild(svgEl("path", { d, fill: color, "fill-opacity": 0.16, stroke: "none" }));
  }
  svg.appendChild(svgEl("line", { class: "axis-line", x1: PAD.l, x2: w - PAD.r, y1: zero, y2: zero }));
  svg.appendChild(svgEl("path", {
    class: "series-line",
    stroke: css("--series-1"),
    d: minutes.map((m, i) => `${i ? "L" : "M"}${X(m)},${Y(gold_delta[i])}`).join(""),
  }));

  // Event markers sit on the rail just above the x axis, shape-coded.
  const railY = h - PAD.b - 6;
  const marks = {
    my_death: { color: css("--critical"), shape: "x", label: "death" },
    my_kill: { color: css("--series-3"), shape: "tri", label: "kill" },
    objective_ours: { color: css("--series-1"), shape: "dia", label: "objective (us)" },
    objective_theirs: { color: css("--series-2"), shape: "dia-open", label: "objective (them)" },
  };
  for (const ev of game.events) {
    const spec = marks[ev.kind];
    if (!spec) continue;
    const x = X(ev.t), g = svgEl("g", { cursor: "pointer" });
    if (spec.shape === "x") {
      g.appendChild(svgEl("path", {
        d: `M${x - 5},${railY - 5}L${x + 5},${railY + 5}M${x + 5},${railY - 5}L${x - 5},${railY + 5}`,
        stroke: spec.color, "stroke-width": 2.5, "stroke-linecap": "round",
      }));
    } else if (spec.shape === "tri") {
      g.appendChild(svgEl("path", {
        d: `M${x},${railY - 6}L${x + 5.5},${railY + 4}L${x - 5.5},${railY + 4}Z`, fill: spec.color,
      }));
    } else {
      g.appendChild(svgEl("path", {
        d: `M${x},${railY - 6}L${x + 6},${railY}L${x},${railY + 6}L${x - 6},${railY}Z`,
        fill: spec.shape === "dia" ? spec.color : "none",
        stroke: spec.color, "stroke-width": 2,
      }));
    }
    g.appendChild(svgEl("rect", { x: x - 9, y: railY - 10, width: 18, height: 20, fill: "transparent" }));
    g.addEventListener("mousemove", (e) =>
      showTip(`<b>${ev.clock}</b> — ${ev.label}`, e.clientX, e.clientY));
    g.addEventListener("mouseleave", hideTip);
    svg.appendChild(g);
  }

  // Crosshair readout across the whole plot.
  const cross = svgEl("line", { class: "axis-line", y1: PAD.t, y2: h - PAD.b, opacity: 0 });
  svg.appendChild(cross);
  const hit = svgEl("rect", {
    x: PAD.l, y: PAD.t, width: w - PAD.l - PAD.r, height: h - PAD.t - PAD.b, fill: "transparent",
  });
  hit.addEventListener("mousemove", (e) => {
    const box = svg.getBoundingClientRect();
    const m = ((e.clientX - box.left) / box.width * w - PAD.l) / (w - PAD.l - PAD.r) * xMax;
    let i = 0;
    minutes.forEach((mm, j) => { if (Math.abs(mm - m) < Math.abs(minutes[i] - m)) i = j; });
    cross.setAttribute("x1", X(minutes[i]));
    cross.setAttribute("x2", X(minutes[i]));
    cross.setAttribute("opacity", 1);
    const g = gold_delta[i];
    showTip(`<b>${clock(minutes[i])}</b><br>team gold ${g > 0 ? "+" : ""}${g}<br>` +
      `my CS ${game.tracks.my_cs[i]}`, e.clientX, e.clientY);
  });
  hit.addEventListener("mouseleave", () => { hideTip(); cross.setAttribute("opacity", 0); });
  svg.appendChild(hit);

  const legend = el("div", "legend");
  legend.innerHTML =
    `<span><span class="swatch" style="background:${css("--ahead")}"></span>team ahead</span>` +
    `<span><span class="swatch" style="background:${css("--behind")}"></span>team behind</span>` +
    Object.values(marks).map((s) => `<span>✕ ${s.label}</span>`.replace("✕",
      s.shape === "x" ? "✕" : s.shape === "tri" ? "▲" : s.shape === "dia" ? "◆" : "◇")).join("");

  return { svg, legend, table: () => tableOf(
    ["min", "team gold", "my CS", "my gold"],
    minutes.map((m, i) => [clock(m), gold_delta[i], game.tracks.my_cs[i], game.tracks.my_gold[i]])) };
}

/** My CS over time, with the final value direct-labeled. */
function csChart(game) {
  const { minutes, my_cs } = game.tracks;
  const w = 960, h = 170;
  const xMax = Math.ceil(minutes[minutes.length - 1]);
  const yMax = Math.max(20, Math.ceil(Math.max(...my_cs) / 20) * 20);
  const X = (m) => PAD.l + (m / xMax) * (w - PAD.l - PAD.r);
  const Y = (v) => PAD.t + (1 - v / yMax) * (h - PAD.t - PAD.b);

  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, role: "img" });
  axes(svg, w, h, xMax, [0, yMax / 2, yMax].map((v) => ({ v, y: Y(v) })), (v) => Math.round(v));
  svg.appendChild(svgEl("path", {
    class: "series-line", stroke: css("--series-1"),
    d: minutes.map((m, i) => `${i ? "L" : "M"}${X(m)},${Y(my_cs[i])}`).join(""),
  }));

  // A flat stretch is dead time — mark the minutes with no farm at all.
  // The first two minutes are the walk-out and first camp, never "dead".
  for (let i = 3; i < my_cs.length; i++) {
    if (my_cs[i] - my_cs[i - 1] > 0) continue;
    const g = svgEl("g", { cursor: "pointer" });
    g.appendChild(svgEl("circle", {
      cx: X(minutes[i]), cy: Y(my_cs[i]), r: 5,
      fill: css("--surface-1"), stroke: css("--critical"), "stroke-width": 2,
    }));
    g.addEventListener("mousemove", (e) => showTip(
      `<b>${clock(minutes[i - 1])}–${clock(minutes[i])}</b><br>no CS this minute`, e.clientX, e.clientY));
    g.addEventListener("mouseleave", hideTip);
    svg.appendChild(g);
  }

  const last = minutes.length - 1;
  const lab = svgEl("text", {
    class: "point-label", x: X(minutes[last]) - 4, y: Y(my_cs[last]) - 10, "text-anchor": "end",
  });
  lab.textContent = `${my_cs[last]} CS`;
  svg.appendChild(lab);

  const legend = el("div", "legend");
  legend.innerHTML = `<span>◯ minute with zero CS</span>`;
  return { svg, legend, table: () => tableOf(["min", "my CS"], minutes.map((m, i) => [clock(m), my_cs[i]])) };
}

function tableOf(head, rows) {
  const t = el("table");
  t.innerHTML = `<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>` +
    rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + "</tbody>";
  return t;
}

function chartCard(title, note, built) {
  const card = el("div", "card");
  const head = el("div", "chart-head");
  head.appendChild(el("h3", null, title));
  if (note) head.appendChild(el("span", "sub", note));
  const btn = el("button", "toggle", "Table");
  head.appendChild(btn);
  card.appendChild(head);
  card.appendChild(built.svg);
  card.appendChild(built.legend);

  const tableWrap = el("div", "hidden");
  tableWrap.appendChild(built.table());
  card.appendChild(tableWrap);
  btn.addEventListener("click", () => {
    const showing = !tableWrap.classList.toggle("hidden");
    built.svg.classList.toggle("hidden", showing);
    built.legend.classList.toggle("hidden", showing);
    btn.textContent = showing ? "Chart" : "Table";
  });
  return card;
}

/* ---------- minimap replay ---------- */

function minimap(game) {
  const card = el("div", "card");
  card.appendChild(el("h3", null, "Minimap replay"));
  card.appendChild(el("p", "sub",
    "Positions come from the API once a minute; kills and objectives are exact."));

  const wrap = el("div", "map-wrap");
  const map = el("div", "map");
  const img = el("img");
  img.src = "assets/map11.png";
  img.alt = "Summoner's Rift minimap";
  map.appendChild(img);
  const svg = svgEl("svg", { viewBox: "0 0 100 100" });
  map.appendChild(svg);
  wrap.appendChild(map);

  const controls = el("div", "map-controls");
  const time = el("div", "clock", "0:00");
  const slider = el("input");
  slider.type = "range";
  slider.min = 0;
  slider.max = game.tracks.positions.length - 1;
  slider.value = 0;
  const play = el("button", "play", "▶ Play");
  const detail = el("div", "sub");
  controls.append(time, slider, play, detail);
  wrap.appendChild(controls);
  card.appendChild(wrap);

  // Game coordinates put y at the bottom; screen space is the other way up.
  const px = (v) => v * 100;
  const py = (v) => (1 - v) * 100;

  const draw = (i) => {
    svg.textContent = "";
    const frame = game.tracks.positions[i];
    time.textContent = clock(frame.t);

    // My path so far.
    const path = game.tracks.positions.slice(0, i + 1)
      .map((f) => f.champs.find((c) => c.me))
      .filter(Boolean)
      .map((c, j) => `${j ? "L" : "M"}${px(c.x)},${py(c.y)}`).join("");
    svg.appendChild(svgEl("path", {
      d: path, fill: "none", stroke: css("--series-1"),
      "stroke-width": 0.8, "stroke-opacity": 0.55, "stroke-linejoin": "round",
    }));

    // Events up to now, shape-coded like the timeline rail.
    for (const ev of game.events.filter((e) => e.t <= frame.t)) {
      const x = px(ev.x), y = py(ev.y);
      const recent = frame.t - ev.t < 1.5;
      if (ev.kind === "my_death") {
        svg.appendChild(svgEl("path", {
          d: `M${x - 1.6},${y - 1.6}L${x + 1.6},${y + 1.6}M${x + 1.6},${y - 1.6}L${x - 1.6},${y + 1.6}`,
          stroke: css("--critical"), "stroke-width": 1, "stroke-opacity": recent ? 1 : 0.5,
          "stroke-linecap": "round",
        }));
      } else if (ev.kind === "my_kill") {
        svg.appendChild(svgEl("path", {
          d: `M${x},${y - 1.8}L${x + 1.6},${y + 1.2}L${x - 1.6},${y + 1.2}Z`,
          fill: css("--series-3"), "fill-opacity": recent ? 1 : 0.5,
        }));
      } else {
        svg.appendChild(svgEl("path", {
          d: `M${x},${y - 1.9}L${x + 1.9},${y}L${x},${y + 1.9}L${x - 1.9},${y}Z`,
          fill: ev.kind === "objective_ours" ? css("--series-1") : "none",
          stroke: ev.kind === "objective_ours" ? "none" : css("--series-2"),
          "stroke-width": 0.9, "fill-opacity": recent ? 1 : 0.45, "stroke-opacity": recent ? 1 : 0.45,
        }));
      }
    }

    // Champions: me and the enemy jungler get their own hues and a ring.
    for (const c of frame.champs) {
      const color = c.me ? css("--series-1") : c.jungler ? css("--series-2")
        : c.ally ? css("--series-1") : css("--muted");
      const g = svgEl("g", { cursor: "pointer" });
      g.appendChild(svgEl("circle", {
        cx: px(c.x), cy: py(c.y), r: c.me || c.jungler ? 2.2 : 1.5,
        fill: color, "fill-opacity": c.me || c.jungler ? 1 : 0.5,
        stroke: css("--surface-1"), "stroke-width": 0.5,
      }));
      g.addEventListener("mousemove", (e) => showTip(
        `<b>${c.name}</b> lvl ${c.level}<br>${c.me ? "you" : c.ally ? "ally" : c.jungler ? "enemy jungler" : "enemy"}`,
        e.clientX, e.clientY));
      g.addEventListener("mouseleave", hideTip);
      svg.appendChild(g);
    }

    const me = frame.champs.find((c) => c.me);
    const ej = frame.champs.find((c) => c.jungler);
    detail.textContent = me
      ? `You: level ${me.level}. Enemy jungler${ej ? `: level ${ej.level}` : " not tracked"}.`
      : "";
  };

  slider.addEventListener("input", () => draw(+slider.value));

  let timer = null;
  play.addEventListener("click", () => {
    if (timer) {
      clearInterval(timer); timer = null; play.textContent = "▶ Play"; return;
    }
    play.textContent = "❚❚ Pause";
    timer = setInterval(() => {
      const next = (+slider.value + 1) % game.tracks.positions.length;
      slider.value = next;
      draw(next);
      if (next === game.tracks.positions.length - 1) {
        clearInterval(timer); timer = null; play.textContent = "▶ Play";
      }
    }, 700);
  });

  const legend = el("div", "legend");
  legend.innerHTML =
    `<span><span class="swatch" style="background:${css("--series-1")}"></span>you / allies</span>` +
    `<span><span class="swatch" style="background:${css("--series-1")};height:2px;border-radius:0"></span>your path so far</span>` +
    `<span><span class="swatch" style="background:${css("--series-2")}"></span>enemy jungler (${game.enemy_jungler})</span>` +
    `<span><span class="swatch" style="background:${css("--muted")}"></span>other enemies</span>` +
    `<span>✕ your death</span><span>▲ your kill</span><span>◆ objective</span>`;
  card.appendChild(legend);

  draw(0);
  return card;
}

/* ---------- video findings (Phase 3) ---------- */

function videoFinding(v) {
  const box = el("div", "video-finding");

  const head = el("div", "vf-head");
  head.appendChild(el("p", "vf-verdict", v.verdict));
  if (v.changes_stats_verdict) {
    head.appendChild(el("span", "badge overturn", "footage overturns the stats"));
  }
  box.appendChild(head);

  if (v.confidence) box.appendChild(el("span", "vf-confidence", `confidence: ${v.confidence}`));

  if (v.observations && v.observations.length) {
    const ul = el("ul", "vf-observations");
    for (const o of v.observations) {
      const li = el("li");
      if (o.clock) li.appendChild(el("span", "vf-clock", o.clock));
      if (o.frames) li.appendChild(el("span", "vf-frames", o.frames));
      li.appendChild(el("span", "vf-saw", o.saw));
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }

  if (v.answers && v.answers.length) {
    const dl = el("dl", "vf-answers");
    for (const qa of v.answers) {
      dl.appendChild(el("dt", null, qa.question));
      dl.appendChild(el("dd", null, qa.answer));
    }
    box.appendChild(dl);
  }

  if (v.unreadable && v.unreadable.length) {
    const p = el("p", "vf-unreadable");
    p.appendChild(el("span", "vf-unreadable-label", "Couldn't tell from the footage: "));
    p.appendChild(document.createTextNode(v.unreadable.join("; ")));
    box.appendChild(p);
  }

  return box;
}

/* ---------- moments ---------- */

function momentCard(game) {
  const card = el("div", "card");
  card.appendChild(el("h3", null, `Key moments (${game.moments.length})`));

  for (const m of game.moments) {
    const box = el("div", "moment");
    const head = el("div", "moment-head");
    head.appendChild(el("span", "when", m.clock));
    head.appendChild(el("span", "type", m.type.replace(/_/g, " ")));
    if (m.needs_video) {
      const b = el("span", "badge", m.clip ? "✓ clip ready" : "needs video");
      b.classList.add(m.clip ? "ok" : "off");
      head.appendChild(b);
    }
    box.appendChild(head);
    box.appendChild(el("p", "why", m.why));

    if (m.clip) {
      const v = el("video");
      v.src = m.clip;
      v.controls = true;
      v.preload = "metadata";
      box.appendChild(v);
      if (m.video) box.appendChild(videoFinding(m.video));
    } else if (m.capture && m.needs_video) {
      box.appendChild(el("div", "pending",
        `Capture pending — ${m.capture.start_s}s to ${m.capture.end_s}s, following ${m.capture.follow}.`));
    }

    if (m.state) {
      const grid = el("div", "state-grid");
      // moments.json marks sides as the strings "ally"/"enemy".
      const side = (s) => (s.team === "ally" ? "" : " (enemy)");
      const rows = Object.entries(m.state).sort(
        ([a, sa], [b, sb]) => (sa.team === sb.team ? a.localeCompare(b) : sa.team === "ally" ? -1 : 1));
      for (const [name, s] of rows) {
        const row = el("div");
        const mine = name === game.meta.champion && s.team === "ally";
        row.appendChild(el("span", "champ", `${name}${mine ? " (you)" : side(s)}`));
        row.appendChild(el("span", null, `${s.zone}, lvl ${s.level}`));
        grid.appendChild(row);
      }
      box.appendChild(grid);
      if (m.state_staleness_s) {
        box.appendChild(el("div", "stale",
          `Positions as of ${m.state_as_of} — up to ${m.state_staleness_s}s before this moment.`));
      }
    }
    card.appendChild(box);
  }
  return card;
}

/* ---------- dashboard ---------- */

function bullet(name, value, target, lowerIsBetter, fmt = (v) => v) {
  const row = el("div", "bullet");
  row.appendChild(el("div", "name", name));
  const track = el("div", "track");
  const scale = Math.max(value, target) * 1.25 || 1;
  const fill = el("div", "fill");
  fill.style.width = `${Math.min(100, (value / scale) * 100)}%`;
  const tick = el("div", "target");
  tick.style.left = `${Math.min(100, (target / scale) * 100)}%`;
  track.append(fill, tick);
  row.appendChild(track);

  const ok = lowerIsBetter ? value <= target : value >= target;
  const val = el("div", "val");
  val.innerHTML = `${fmt(value)} <span class="badge ${ok ? "ok" : "off"}">` +
    `${ok ? "✓ on target" : "✗ off target"}</span>`;
  row.appendChild(val);
  return row;
}

function dashboard() {
  const frag = document.createDocumentFragment();
  const b = D.batch;

  const head = el("div", "card");
  head.appendChild(el("h3", null, `Batch: ${b.games} games, ${b.record}`));
  head.appendChild(el("p", "sub",
    "Bars show the batch average; the vertical mark is the target from profile.md."));
  const map = [
    ["Deaths before 15 min", b.avg_deaths_before_15, "deaths_before_15"],
    ["Solo deaths", b.avg_solo_deaths, "solo_deaths"],
    ["CS per minute", b.avg_cs_per_min, "cs_per_min"],
    ["Objective presence", b.avg_objective_presence, "objectives_i_was_present_for"],
    ["Kill participation", b.avg_kill_participation, "kill_participation"],
    ["Wards per minute", b.avg_wards_per_min, "wards_per_min"],
  ];
  for (const [label, value, key] of map) {
    const t = D.targets[key];
    if (value == null || !t) continue;
    head.appendChild(bullet(label, value, t.target, t.lower_is_better,
      key === "kill_participation" ? (v) => `${Math.round(v * 100)}%` : (v) => v));
  }
  frag.appendChild(head);

  const perGame = el("div", "card");
  perGame.appendChild(el("h3", null, "Per game"));
  perGame.appendChild(tableOf(
    ["game", "result", "vs jungler", "lobby", "K/D/A", "CS/min", "deaths pre-15", "obj presence", "wards/min"],
    D.games.map((g) => {
      const s = g.stats;
      return [g.meta.match_id.replace("NA1_", ""), s.result, s.enemy_jungler,
        s.lobby ? s.lobby.enemy_avg_rank : "—", s.kda,
        s.cs_per_min, s.deaths_before_15,
        `${s.objectives_i_was_present_for}/${s.objectives_team_took}`, s.wards_per_min];
    })));
  frag.appendChild(perGame);

  if (D.batch_review_md) {
    const rv = el("div", "card md");
    rv.innerHTML = markdown(D.batch_review_md);
    frag.appendChild(rv);
  }
  for (const [title, md] of [["Goals", D.profile_md], ["Patterns", D.patterns_md]]) {
    const c = el("div", "card md");
    c.innerHTML = markdown(md);
    c.insertBefore(el("h2", null, title), c.firstChild);
    frag.appendChild(c);
  }
  return frag;
}

/* ---------- queue (review + video selection) ---------- */

// In-memory selection, keyed by match_id, seeded once from each game's saved
// `queued` flags. Kept at module scope so it survives switching tabs.
const queueState = {};

const RELATIVE_DAY = (g) => {
  // Deliberately NOT meta.age_days: that is elapsed whole days in UTC, so a
  // game played last night reads as 0 and renders "today".
  const d = g.played_utc ? daysAgoLocal(g.played_utc) : null;
  if (d == null) return g.played_utc ? localYMD(g.played_utc) : "—";
  if (d <= 0) return "today";
  if (d === 1) return "yesterday";
  if (d < 7) return `${d}d ago`;
  return localMD(g.played_utc);
};

const queueDuration = (g) => (g.duration_s != null ? clock(g.duration_s / 60) : "—");

function queueStatusCell(g) {
  const wrap = el("div");
  wrap.appendChild(el("span", `badge ${g.has_review ? "ok" : "off"}`, g.has_review ? "reviewed" : "no review"));
  const bits = [];
  if (g.moments != null) bits.push(`${g.moments} moment${g.moments === 1 ? "" : "s"}`);
  if (g.clips_planned) bits.push(`${g.clips_captured ?? 0}/${g.clips_planned} clips`);
  if (g.video_findings) bits.push(`${g.video_findings} finding${g.video_findings === 1 ? "" : "s"}`);
  if (bits.length) wrap.appendChild(el("span", "sub", " " + bits.join(" · ")));
  return wrap;
}

function queueExpiryCell(g) {
  const capturable = g.replay_capturable !== false;
  if (!capturable) {
    const b = el("span", "badge off", "expired");
    b.title = "Replay is past the patch cutoff — video can no longer be captured for this game.";
    return b;
  }
  if (g.age_days != null && g.age_days > 12) {
    const b = el("span", "badge warn", "expiring soon");
    b.title = `Replay is ${g.age_days} days old. Replays stop opening once the patch rotates — capture soon.`;
    return b;
  }
  return el("span", "sub", "—");
}

/** One <tr> for a game, wired to `state` (queueState[match_id]). Returns the
 *  row plus its two checkbox inputs so bulk actions can sync them. */
function queueRow(g, state, onChange) {
  const tr = el("tr");
  const td = (content, cls) => {
    const c = el("td", cls);
    if (content instanceof Node) c.appendChild(content);
    else c.textContent = content;
    tr.appendChild(c);
    return c;
  };

  const playedTd = td(RELATIVE_DAY(g));
  if (g.played_utc) playedTd.title = localStamp(g.played_utc);

  td(`${g.champion ?? "—"}${g.position ? " " + g.position.toLowerCase() : ""}`);

  const wlClass = g.win == null ? "badge" : `badge ${g.win ? "ok" : "off"}`;
  td(el("span", wlClass, g.win == null ? "—" : g.win ? "W" : "L"));

  td(g.kda ?? "—");
  td(g.cs != null ? String(g.cs) : "—");
  td(queueDuration(g));
  td(g.enemy_jungler ?? "—");
  td(queueStatusCell(g));
  td(queueExpiryCell(g));

  const capturable = g.replay_capturable !== false;

  const reviewCb = el("input");
  reviewCb.type = "checkbox";
  reviewCb.checked = !!state.review;
  reviewCb.addEventListener("change", () => { state.review = reviewCb.checked; onChange(); });
  td(reviewCb, "cb");

  const videoCb = el("input");
  videoCb.type = "checkbox";
  videoCb.checked = !!state.video;
  videoCb.disabled = !capturable;
  const videoTd = td(videoCb, "cb");
  if (!capturable) videoTd.title = "Replay no longer capturable — video is not possible for this game.";

  return { tr, inputs: { review: reviewCb, video: videoCb } };
}

/** Shell commands the current selection implies. */
function queueCommand(games, state) {
  const reviewIds = games.filter((g) => state[g.match_id]?.review).map((g) => g.match_id);
  const videoIds = games.filter((g) => state[g.match_id]?.video).map((g) => g.match_id);
  const lines = [];
  if (reviewIds.length) lines.push("python tools/run_queue.py");
  if (videoIds.length) lines.push(`python tools/capture.py ${videoIds.join(" ")} --launch`);
  return { text: lines.length ? lines.join("\n") : "# nothing queued yet — tick a box above", reviewIds, videoIds };
}

/** The calm, load-bearing warning about capture freezes. Only shown when
 *  video is actually being requested. */
function queueVideoWarning(videoIds, games) {
  if (!videoIds.length) return null;
  const totalClips = videoIds.reduce((sum, id) => {
    const g = games.find((x) => x.match_id === id);
    return sum + (g?.clips_planned || g?.needs_video_moments || 1);
  }, 0);
  const minutes = Math.round(totalClips * 2.5);
  const p = el("p", "queue-warning",
    `Capturing ${totalClips} clip${totalClips === 1 ? "" : "s"} across ${videoIds.length} game${videoIds.length === 1 ? "" : "s"} ` +
    `takes about ${minutes} min total (~2.5 min per clip). Each clip freezes the game 21-35 seconds — ` +
    `do not touch the mouse while it runs. To confirm it is working rather than stuck, watch the frame count ` +
    `rise: find /tmp/lol-coach-capture -name '*.png' | wc -l`);
  return p;
}

async function queueSave(games, btn, statusEl) {
  const payload = {};
  for (const g of games) {
    payload[g.match_id] = { review: !!queueState[g.match_id].review, video: !!queueState[g.match_id].video };
  }
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  statusEl.textContent = "";
  statusEl.className = "queue-status";
  try {
    const res = await fetch("/api/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    await res.json().catch(() => null);
    statusEl.textContent = "Saved.";
    statusEl.classList.add("ok");
  } catch (err) {
    statusEl.textContent = `Couldn't save (${err.message}). Run the command below instead.`;
    statusEl.classList.add("off");
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function queueView() {
  const frag = document.createDocumentFragment();
  const games = Array.isArray(D.library) ? D.library : null;

  if (!games || games.length === 0) {
    const card = el("div", "card");
    card.appendChild(el("h3", null, "Queue"));
    card.appendChild(el("p", "sub",
      "No fetched-game library yet. Run tools/fetch_match.py and tools/build_review.py, then reload this page."));
    frag.appendChild(card);
    return frag;
  }

  for (const g of games) {
    if (!queueState[g.match_id]) {
      const capturable = g.replay_capturable !== false;
      queueState[g.match_id] = {
        review: !!(g.queued && g.queued.review),
        video: capturable && !!(g.queued && g.queued.video),
      };
    }
  }

  const card = el("div", "card");
  card.appendChild(el("h3", null, `Queue (${games.length} game${games.length === 1 ? "" : "s"})`));
  card.appendChild(el("p", "sub",
    "Tick which games get a written review and which get replay clips captured, then save."));

  const bulk = el("div", "queue-bulk");
  const bulkBtn = (label, fn) => {
    const b = el("button", "toggle", label);
    b.addEventListener("click", fn);
    return b;
  };
  bulk.appendChild(el("span", "sub", "Review:"));
  bulk.appendChild(bulkBtn("All", () => setAll("review", true)));
  bulk.appendChild(bulkBtn("None", () => setAll("review", false)));
  bulk.appendChild(el("span", "sub", "Video:"));
  bulk.appendChild(bulkBtn("All capturable", () => setAll("video", true)));
  bulk.appendChild(bulkBtn("None", () => setAll("video", false)));
  card.appendChild(bulk);

  const table = el("table");
  const headRow = el("tr");
  for (const h of ["played", "game", "result", "kda", "cs", "time", "vs jungler", "status", "expiry", "review", "video"]) {
    headRow.appendChild(el("th", null, h));
  }
  const thead = el("thead");
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el("tbody");
  table.appendChild(tbody);

  const rowInputs = {};
  for (const g of games) {
    const { tr, inputs } = queueRow(g, queueState[g.match_id], () => refresh());
    rowInputs[g.match_id] = inputs;
    tbody.appendChild(tr);
  }
  card.appendChild(table);
  frag.appendChild(card);

  const bottom = el("div", "card");
  const countLine = el("p");
  bottom.appendChild(countLine);

  const isFile = location.protocol === "file:";
  const saveWrap = el("div", "queue-save");
  if (isFile) {
    const note = el("p", "sub", "This page is open from a file, so saving is disabled. Serve it instead: ");
    note.appendChild(el("code", null, "python3 -I tools/serve.py 8777"));
    saveWrap.appendChild(note);
  } else {
    const saveBtn = el("button", "play", "Save queue");
    const statusEl = el("span", "queue-status");
    saveBtn.addEventListener("click", () => queueSave(games, saveBtn, statusEl));
    saveWrap.append(saveBtn, statusEl);
  }
  bottom.appendChild(saveWrap);

  const warnWrap = el("div");
  bottom.appendChild(warnWrap);

  const cmdHead = el("div", "chart-head");
  cmdHead.appendChild(el("h3", null, "Command to run"));
  const copyBtn = el("button", "toggle", "Copy");
  cmdHead.appendChild(copyBtn);
  bottom.appendChild(cmdHead);
  const pre = el("pre", "cmdblock");
  bottom.appendChild(pre);
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(pre.textContent);
      const old = copyBtn.textContent;
      copyBtn.textContent = "Copied";
      setTimeout(() => (copyBtn.textContent = old), 1200);
    } catch {
      copyBtn.textContent = "Select & copy manually";
    }
  });
  frag.appendChild(bottom);

  function setAll(kind, value) {
    for (const g of games) {
      // Nothing to film in a game with no detected moments (a remake, or one
      // that hasn't been through find_moments yet), so bulk-select skips it.
      // An individual checkbox can still be ticked by hand.
      if (kind === "video" && value &&
          (g.replay_capturable === false || !g.moments)) continue;
      queueState[g.match_id][kind] = value;
      const input = rowInputs[g.match_id][kind];
      if (input && !input.disabled) input.checked = value;
    }
    refresh();
  }

  function refresh() {
    const reviewCount = games.filter((g) => queueState[g.match_id].review).length;
    const videoCount = games.filter((g) => queueState[g.match_id].video).length;
    countLine.textContent = `${reviewCount} game${reviewCount === 1 ? "" : "s"} queued for review, ${videoCount} for video.`;

    const { text, videoIds } = queueCommand(games, queueState);
    pre.textContent = text;

    warnWrap.textContent = "";
    const warn = queueVideoWarning(videoIds, games);
    if (warn) warnWrap.appendChild(warn);
  }

  refresh();
  return frag;
}

/* ---------- routing ---------- */

const main = el("div", "wrap");
const nav = el("nav");

// Ranks are fetched after the game, so they describe the lobby as of that
// date; an average over one or two ranked players is flagged as thin.
function lobbyLine(l) {
  const ej = l.enemy_jungler;
  return `Lobby: enemies average ${l.enemy_avg_rank} (${l.enemies_ranked}/5 ranked` +
    `${l.confident ? "" : ", thin"}) — ${l.verdict}` +
    (ej ? ` · enemy jungler ${ej.champion} ${ej.rank}` : "") +
    ` · ranks as of ${l.ranks_as_of}`;
}

function gameView(game) {
  const frag = document.createDocumentFragment();
  const s = game.stats;
  const top = el("div", "card");
  top.appendChild(el("h2", null,
    `${game.meta.champion} ${game.meta.position.toLowerCase()} — ${s.result}, ` +
    `${Math.round(s.duration_min)} min vs ${s.enemy_jungler}`));
  top.appendChild(el("p", "sub",
    `${game.meta.queue} · ${localYMD(game.meta.played_utc)} · ` +
    (game.meta.replay_capturable ? "replay still capturable" : "replay expired")));
  if (s.lobby) top.appendChild(el("p", "sub", lobbyLine(s.lobby)));
  const strip = el("div", "stats");
  for (const [k, v] of [["K/D/A", s.kda], ["CS/min", s.cs_per_min],
    ["Deaths pre-15", s.deaths_before_15], ["Objectives", `${s.objectives_i_was_present_for}/${s.objectives_team_took}`],
    ["Kill part.", `${Math.round(s.kill_participation * 100)}%`], ["Vision", s.vision_score],
    ["Lobby", s.lobby ? s.lobby.enemy_avg_rank : "—"]]) {
    const st = el("div", "stat");
    st.appendChild(el("div", "k", k));
    st.appendChild(el("div", "v", String(v)));
    strip.appendChild(st);
  }
  top.appendChild(strip);
  frag.appendChild(top);

  frag.appendChild(chartCard("Team gold lead", "positive means your team is ahead", goldChart(game)));
  frag.appendChild(chartCard("Your CS", "flat stretches are dead time", csChart(game)));
  frag.appendChild(minimap(game));
  frag.appendChild(momentCard(game));

  if (game.review_md) {
    const rv = el("div", "card md");
    rv.innerHTML = markdown(game.review_md);
    frag.appendChild(rv);
  } else {
    const rv = el("div", "card");
    rv.appendChild(el("p", "sub", "No written review yet for this game."));
    frag.appendChild(rv);
  }
  return frag;
}

function render(route) {
  main.textContent = "";
  for (const btn of nav.children) btn.setAttribute("aria-current", String(btn.dataset.route === route));
  main.appendChild(route === "batch" ? dashboard()
    : route === "queue" ? queueView()
    : gameView(D.games.find((g) => g.meta.match_id === route)));
  window.scrollTo({ top: 0 });
}

function init() {
  const header = document.querySelector("header .wrap");
  header.appendChild(Object.assign(el("h1", null, "lol-coach"), {}));
  header.appendChild(el("span", "sub", D.player));

  const routes = [["batch", "Batch"], ["queue", "Queue"]].concat(D.games.map((g) => [
    g.meta.match_id,
    `${g.stats.result === "win" ? "W" : "L"} ${g.meta.champion} ${localMD(g.meta.played_utc)}`,
  ]));
  for (const [route, label] of routes) {
    const btn = el("button", null, label);
    btn.dataset.route = route;
    btn.addEventListener("click", () => render(route));
    nav.appendChild(btn);
  }
  header.appendChild(nav);
  document.body.appendChild(main);
  render("batch");
}

init();
