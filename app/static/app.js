// Daedalus Web. Model replies and program output are inserted as text only; the small
// formatter below builds elements (code blocks, inline code, bold) without innerHTML.
"use strict";

let current = null;
const composer = $("#composer");
const box = composer.elements.text;

// ```code``` blocks, `inline code` and **bold**, everything else as plain text.
function formatted(text) {
  const out = el("div", { class: "md" });
  const parts = text.split(/```(\w*)\n?([\s\S]*?)```/g);
  for (let i = 0; i < parts.length; i += 3) {
    for (const para of parts[i].split(/\n{2,}/)) {
      if (!para.trim()) continue;
      const p = el("p");
      for (const piece of para.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g)) {
        if (piece.startsWith("`") && piece.endsWith("`") && piece.length > 2) p.append(el("code", {}, piece.slice(1, -1)));
        else if (piece.startsWith("**") && piece.endsWith("**") && piece.length > 4) p.append(el("b", {}, piece.slice(2, -2)));
        else p.append(piece);
      }
      out.append(p);
    }
    if (i + 2 < parts.length) out.append(el("pre", { class: "code" }, el("code", {}, parts[i + 2].replace(/\n$/, ""))));
  }
  return out;
}

function runCard(run, n) {
  const ok = run.exit_code === 0;
  const lines = run.code.split("\n").filter(Boolean).length;
  return el("div", { class: "run" },
    el("div", { class: "run__head" },
      el("span", { class: `badge ${ok ? "badge--good" : "badge--bad"}` }, ok ? "exit 0" : run.timed_out ? "killed" : `exit ${run.exit_code}`),
      el("span", {}, `Sandbox run ${n}`),
      el("span", { class: "note mono" }, `${run.seconds.toFixed(2)} s`)),
    el("pre", { class: `output${ok ? "" : " output--bad"}` }, run.output),
    el("details", { class: "run__code" },
      el("summary", {}, `Code that ran (${lines} lines)`),
      el("pre", { class: "code" }, el("code", {}, run.code.replace(/\n$/, "")))));
}

function message(item) {
  if (item.role === "user") return el("div", { class: "msg msg--user" }, el("div", { class: "bubble" }, item.text));
  const node = el("div", { class: `msg msg--${item.role}` },
    el("img", { class: "msg__avatar", src: "/static/daedalus.svg", alt: "" }),
    el("div", { class: "msg__body" },
      item.role === "error" ? el("p", { class: "error" }, item.text) : formatted(item.text),
      (item.runs || []).map((r, i) => runCard(r, i + 1)),
      el("p", { class: "note mono msg__meta" }, `${item.seconds ?? "–"} s${item.runs?.length ? ` · ${item.runs.length} sandbox run${item.runs.length > 1 ? "s" : ""}` : ""}`)));
  return node;
}

function drawFiles(files) {
  $("#files").replaceChildren(...(files.length ? files.map((f) =>
    el("li", {}, el("a", { href: `/api/chats/${current}/files/${encodeURIComponent(f.name)}`, download: f.name }, f.name),
      el("span", { class: "note mono" }, f.bytes < 1024 ? `${f.bytes} B` : `${(f.bytes / 1024).toFixed(1)} kB`)))
    : [el("li", { class: "note" }, "No files yet.")]));
}

async function loadState() {
  const s = await api("/api/state");
  $("#model").textContent = `${s.model} · ${s.effort}`;
  $("#model").hidden = s.demo;
  $("#demo").hidden = !s.demo;
  $("#sandbox").textContent = s.docker ? `Docker sandbox · ${s.timeout} s per run` : "Docker not found: code cannot run";
  $("#chats").replaceChildren(...s.chats.map((c) => el("li", {},
    el("button", { type: "button", class: "chat-btn", "aria-current": String(c.id === current), onclick: () => open(c.id) },
      el("span", { class: "chat-btn__title" }, c.title), el("small", { class: "note" }, `${Math.ceil(c.messages / 2)} question${c.messages > 2 ? "s" : ""}`)),
    el("button", { type: "button", class: "chat-del", "aria-label": `Delete ${c.title}`, onclick: () => remove(c.id) }, "×"))));
  return s;
}

async function open(id) {
  current = id;
  const chat = await api(`/api/chats/${id}`);
  const list = $("#messages");
  list.replaceChildren(...(chat.items.length ? chat.items.map(message)
    : [el("div", { class: "empty" }, el("p", {}, "Ask anything about code."), el("p", { class: "muted" }, "When running code helps, Daedalus runs it in the sandbox and shows you exactly what ran."))]));
  list.scrollTop = list.scrollHeight;
  drawFiles(chat.files);
  await loadState();
  box.focus();
}

async function newChat() {
  const chat = await api("/api/chats", {});
  await open(chat.id);
}

async function remove(id) {
  if (!confirm("Delete this chat from the list? Its folder stays on disk.")) return;
  await api(`/api/chats/${id}/delete`, {});
  const s = await loadState();
  if (id === current) { if (s.chats.length) open(s.chats[0].id); else newChat(); }
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = box.value.trim();
  if (!text || current == null) return;
  const list = $("#messages");
  list.querySelector(".empty")?.remove();
  list.append(message({ role: "user", text }));
  const thinking = el("div", { class: "msg msg--assistant" }, el("img", { class: "msg__avatar", src: "/static/daedalus.svg", alt: "" }),
    el("div", { class: "msg__body" }, el("p", { class: "typing" }, "Thinking and running code…")));
  list.append(thinking);
  list.scrollTop = list.scrollHeight;
  box.value = "";
  $("#send").disabled = true;
  try {
    const r = await api(`/api/chats/${current}/ask`, { text });
    thinking.replaceWith(message(r.item));
    drawFiles(r.files);
    loadState();
  } catch (error) {
    thinking.replaceWith(message({ role: "error", text: error.message }));
  } finally {
    $("#send").disabled = false;
    list.scrollTop = list.scrollHeight;
  }
});
box.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); }
});
$("#new").addEventListener("click", newChat);

loadState().then((s) => (s.chats.length ? open(s.chats[0].id) : newChat())).catch((error) => toast(error.message));
