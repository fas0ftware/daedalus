// Small helpers shared by the FA Software web interfaces.
// Values from the server are only ever inserted as text (textContent), never as HTML.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function toast(text) {
  const node = el("div", { class: "toast", role: "status" }, text);
  document.body.append(node);
  setTimeout(() => node.remove(), 2600);
}

// A small line chart as inline SVG (values: numbers, oldest first).
function sparkline(values, { color = "currentColor", target = null, height = 44 } = {}) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 200 ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  if (values.length < 2) return svg;
  const all = target == null ? values : values.concat([target]);
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const y = (v) => (height - 4 - ((v - lo) / span) * (height - 8)).toFixed(1);
  const x = (i) => ((i / (values.length - 1)) * 200).toFixed(1);
  if (target != null) {
    const line = document.createElementNS(NS, "line");
    Object.entries({ x1: 0, x2: 200, y1: y(target), y2: y(target), stroke: "currentColor", "stroke-dasharray": "3 4", "stroke-width": 1, opacity: 0.45, "vector-effect": "non-scaling-stroke" })
      .forEach(([k, v]) => line.setAttribute(k, v));
    svg.append(line);
  }
  const path = document.createElementNS(NS, "polyline");
  path.setAttribute("points", values.map((v, i) => `${x(i)},${y(v)}`).join(" "));
  Object.entries({ fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" })
    .forEach(([k, v]) => path.setAttribute(k, v));
  svg.append(path);
  return svg;
}
