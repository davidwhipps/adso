/*
 * Adso web UI behaviour. Plain JS on top of htmx + Alpine, no build step.
 *
 * The server renders everything; this script adds the parts that need a
 * browser: the masonry layout, the book sidebar (fetched from
 * /book/{id}/inspect), keyboard navigation, multi-select with bulk actions,
 * and the view transitions between states.
 */
(() => {
  "use strict";

  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const store = {
    get(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };
  const session = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };
  // Run `fn` inside a view transition where supported. A transition that is
  // skipped (e.g. you navigate mid-animation) just applies the change, so its
  // rejected `ready` promise is expected and swallowed.
  const vt = (fn) => {
    if (!document.startViewTransition) { fn(); return null; }
    const t = document.startViewTransition(fn);
    t.ready.catch(() => {});
    return t;
  };
  const ICON_CHECK = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m2.5 6.5 2.5 2.5 4.5-5.5"/></svg>';

  /* ------------------------------------------------------------ shared */
  function toast(msg, isError = false) {
    const t = $("#toast");
    if (!t) return;
    t.innerHTML = (isError ? "" : ICON_CHECK) + esc(msg);
    t.classList.toggle("err", isError);
    t.classList.add("show");
    clearTimeout(t._t);
    t._t = setTimeout(() => t.classList.remove("show"), 2400);
  }

  function initTheme() {
    $("#themeBtn")?.addEventListener("click", () => {
      const next = !document.documentElement.classList.contains("dark");
      vt(() => document.documentElement.classList.toggle("dark", next));
      store.set("adso_theme", next ? "dark" : "light");
    });
  }

  // Types a suggestion drawn from your own library into the empty search box.
  function initRotator() {
    const rot = $("#rot");
    if (!rot) return;
    const pool = [
      ...$$(".it .cap .a span:first-child").map((e) => e.textContent.trim()),
      ...$$("#sideTags [data-tag]").map((e) => "#" + e.dataset.tag),
    ].filter(Boolean);
    const words = pool.length ? [...new Set(pool)] : ["Bolaño", "Ursula K. Le Guin", "#fiction", "Middlemarch"];
    let word = "", n = 0;
    const next = () => { word = words[Math.floor(Math.random() * words.length)]; n = 0; type(); };
    const type = () => {
      rot.innerHTML = `Try <b>${esc(word.slice(0, n))}</b>${n < word.length ? '<span class="caret"></span>' : ""}`;
      if (n++ < word.length) setTimeout(type, 45 + Math.random() * 50);
      else setTimeout(next, 3200);
    };
    next();
  }

  function initSearch(onChange) {
    const form = $("#search"), q = $("#q");
    if (!form || !q) return;
    const sync = () => form.classList.toggle("has", !!q.value);
    sync();
    let timer;
    q.addEventListener("input", () => {
      sync();
      if (!onChange) return;
      clearTimeout(timer);
      timer = setTimeout(() => onChange(q.value.trim()), 180);
    });
    form.addEventListener("submit", (e) => {
      if (!onChange) return; // other pages: a normal GET to the library
      e.preventDefault();
      clearTimeout(timer);
      onChange(q.value.trim());
      q.blur();
    });
    $("#clr")?.addEventListener("click", () => { q.value = ""; sync(); onChange ? onChange("") : form.submit(); q.focus(); });
  }

  /* ------------------------------------------------------- popovers */
  let popEl = null;
  function closePop() { popEl?.remove(); popEl = null; }
  function placePop(pop, anchor) {
    pop.classList.add("floatpop");
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect(), pr = pop.getBoundingClientRect();
    let top = r.bottom + 8;
    if (top + pr.height > innerHeight - 12) top = r.top - pr.height - 8;
    pop.style.left = Math.max(12, Math.min(r.left, innerWidth - pr.width - 12)) + "px";
    pop.style.top = top + "px";
    popEl = pop;
  }
  function menu(anchor, options, onPick) {
    closePop();
    const pop = document.createElement("div");
    pop.className = "pop";
    pop.innerHTML = options.map(([v, label]) => `<button type="button" data-v="${esc(v)}">${esc(label)}</button>`).join("");
    pop.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { closePop(); onPick(b.dataset.v); } });
    placePop(pop, anchor);
  }
  function askTag(anchor, onPick) {
    closePop();
    const all = JSON.parse($("#allTags")?.textContent || "[]");
    const pop = document.createElement("div");
    pop.className = "pop";
    pop.style.width = "260px";
    pop.innerHTML = '<input placeholder="Find or create a tag" autocomplete="off" spellcheck="false"><div class="sugg"></div>';
    const inp = $("input", pop), sugg = $(".sugg", pop);
    let hi = 0, opts = [];
    const draw = () => {
      const q = inp.value.trim().toLowerCase().replace(/^#/, "");
      opts = all.filter((t) => !q || t.includes(q)).slice(0, 7).map((t) => [t, false]);
      if (q && !all.includes(q)) opts.push([q, true]);
      hi = Math.min(hi, Math.max(0, opts.length - 1));
      sugg.innerHTML = (opts.length ? "" : '<div class="hint">Type to create a tag</div>') +
        opts.map(([t, isNew], i) => `<button type="button" data-t="${esc(t)}" class="${i === hi ? "hi" : ""}">${isNew ? `Create “${esc(t)}”` : "#" + esc(t)}</button>`).join("");
    };
    inp.addEventListener("input", () => { hi = 0; draw(); });
    inp.addEventListener("keydown", (e) => {
      // Keys typed here belong to the picker, not the library's shortcuts
      // (an Enter reaching them would open the focused book).
      e.stopPropagation();
      if (e.key === "ArrowDown") { hi = Math.min(opts.length - 1, hi + 1); draw(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { hi = Math.max(0, hi - 1); draw(); e.preventDefault(); }
      else if (e.key === "Enter") { e.preventDefault(); const t = opts[hi]?.[0]; if (t) { closePop(); onPick(t); } }
      else if (e.key === "Escape") { e.stopPropagation(); closePop(); }
    });
    sugg.addEventListener("mousedown", (e) => { const b = e.target.closest("[data-t]"); if (b) { e.preventDefault(); closePop(); onPick(b.dataset.t); } });
    draw();
    placePop(pop, anchor);
    inp.focus();
  }
  document.addEventListener("mousedown", (e) => {
    if (popEl && !popEl.contains(e.target) && !e.target.closest("[data-bulk]")) closePop();
    $$("details.sortmenu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });

  /* ============================================================ library */
  function initLibrary() {
    const shell = $("#shell");
    const S = { focus: -1, sel: new Set(), open: new URLSearchParams(location.search).get("book") || null, pos: [] };
    const inspIn = $("#inspIn");
    const items = () => $$("#content [data-i]");
    const itemById = (id) => $(`#content [data-id="${CSS.escape(id)}"]`);
    const view = () => $("#browse")?.dataset.view || "grid";

    /* ---- navigation: swap #browse for a new library URL ---- */
    function navigate(url, { push = true, quiet = false } = {}) {
      const main = $("#main"), keep = main?.scrollTop || 0;
      const u = new URL(url, location.origin);
      if (S.open) u.searchParams.set("book", S.open); else u.searchParams.delete("book");
      const target = u.pathname + u.search;
      if (quiet) document.documentElement.classList.add("quiet");
      return htmx.ajax("GET", target, { target: "#browse", select: "#browse", swap: "outerHTML" }).then(() => {
        if (push) history.pushState({}, "", target); else history.replaceState({}, "", target);
        if (quiet) { $("#main").scrollTop = keep; requestAnimationFrame(() => document.documentElement.classList.remove("quiet")); }
      });
    }
    const refresh = () => navigate(location.pathname + location.search, { push: false, quiet: true });
    const withParam = (key, value) => {
      const u = new URL(location.href);
      value ? u.searchParams.set(key, value) : u.searchParams.delete(key);
      return u.pathname + u.search;
    };

    /* ---- per-swap setup ---- */
    function initBrowse() {
      S.focus = -1;
      S.sel.clear();
      renderSelection();
      syncViewSeg();
      const titles = store.get("adso_titles") === "1";
      $("#browse")?.classList.toggle("show-titles", titles);
      $("#titlesBtn")?.classList.toggle("on", titles);
      const size = $("#size");
      if (size) size.value = store.get("adso_size", "190");
      $$("#content img").forEach((img) => {
        if (img.complete && img.naturalWidth) img.classList.add("ld");
        else img.addEventListener("load", () => img.classList.add("ld"), { once: true });
      });
      layout(true);
      markOpen();
      // Remember this list so the book page can offer previous / next.
      session.set("adso:list", { url: location.pathname + location.search, ids: items().map((e) => e.dataset.id).filter(Boolean) });
    }

    function syncViewSeg() {
      const seg = $("#views");
      if (!seg) return;
      $$("button", seg).forEach((b) => b.classList.toggle("on", b.dataset.view === view()));
      placeThumb(seg);
    }

    /* ---- masonry ---- */
    function layout(fresh = false) {
      const m = $("#masonry");
      if (!m) return;
      if (fresh) m.classList.remove("ready");
      m.classList.add("js-layout");
      const W = m.clientWidth, G = W < 600 ? 12 : 22;
      const size = +store.get("adso_size", "190");
      const cols = Math.max(2, Math.round((W + G) / (size + G)));
      const cw = (W - G * (cols - 1)) / cols;
      const titles = $("#browse")?.classList.contains("show-titles");
      const extra = titles ? 52 : 0, gapY = titles ? 14 : G;
      const hs = new Array(cols).fill(0);
      S.pos = [];
      $$(".it", m).forEach((el, i) => {
        let c = 0;
        for (let k = 1; k < cols; k++) if (hs[k] < hs[c] - 1) c = k;
        const h = cw * +el.dataset.ar + extra;
        el.style.width = cw + "px";
        el.style.translate = `${c * (cw + G)}px ${hs[c]}px`;
        S.pos[i] = { c, y: hs[c], h };
        hs[c] += h + gapY;
      });
      m.style.height = Math.max(0, ...hs) + "px";
      if (fresh) requestAnimationFrame(() => requestAnimationFrame(() => m.classList.add("ready")));
    }

    /* ---- marks: focus / selection / open ---- */
    function markFocus() { items().forEach((el, i) => el.classList.toggle("focus", i === S.focus)); }
    function markOpen() { items().forEach((el) => el.classList.toggle("open", !!S.open && el.dataset.id === S.open)); }
    function renderSelection() {
      items().forEach((el) => {
        const on = S.sel.has(el.dataset.id);
        el.classList.toggle("sel", on);
        const btn = $("[data-sel]", el);
        if (btn) btn.innerHTML = on ? ICON_CHECK + "Selected" : "Select";
        const cb = $("[data-cb]", el);
        if (cb) cb.checked = on;
      });
      const all = $("#selAll");
      if (all) all.checked = S.sel.size > 0 && items().every((el) => !el.dataset.id || S.sel.has(el.dataset.id));
      const bar = $("#bulk");
      bar?.classList.toggle("show", S.sel.size > 0);
      if ($("#bulkCount")) $("#bulkCount").textContent = `${S.sel.size} selected`;
    }
    function toggleSel(id) { if (!id) return; S.sel.has(id) ? S.sel.delete(id) : S.sel.add(id); renderSelection(); }

    function scrollToItem(i, block = "nearest") {
      const main = $("#main"), el = items()[i];
      if (!main || !el) return;
      if (view() === "grid" && S.pos[i]) {
        const top = $("#masonry").offsetTop + S.pos[i].y, h = S.pos[i].h, vh = main.clientHeight;
        if (block === "center") main.scrollTop = top - (vh - h) / 2;
        else if (top < main.scrollTop + 10) main.scrollTo({ top: top - 20, behavior: "smooth" });
        else if (top + h > main.scrollTop + vh - 10) main.scrollTo({ top: top + h - vh + 30, behavior: "smooth" });
      } else el.scrollIntoView({ block });
    }
    function setFocus(i) {
      const list = items();
      if (!list.length) return;
      S.focus = Math.max(0, Math.min(list.length - 1, i));
      markFocus();
      scrollToItem(S.focus);
      if (S.open && list[S.focus].dataset.id) openInsp(list[S.focus].dataset.id, { animate: false });
    }
    function moveVert(d) {
      if (view() !== "grid") {
        const cols = view() === "wall" ? getComputedStyle($("#wall")).gridTemplateColumns.split(" ").length : 1;
        return setFocus(S.focus < 0 ? 0 : S.focus + d * cols);
      }
      if (S.focus < 0) return setFocus(0);
      const p = S.pos[S.focus];
      let best = -1, by = d > 0 ? Infinity : -Infinity;
      S.pos.forEach((q, i) => { if (q.c === p.c && (d > 0 ? q.y > p.y && q.y < by : q.y < p.y && q.y > by)) { best = i; by = q.y; } });
      if (best >= 0) setFocus(best);
    }

    /* ---- book sidebar ---- */
    let inspReq = 0;
    async function openInsp(id, { animate = true } = {}) {
      if (!id || !inspIn) return;
      const req = ++inspReq;
      const res = await fetch(`/book/${encodeURIComponent(id)}/inspect`);
      if (req !== inspReq) return; // a newer request superseded this one
      if (!res.ok) return toast("Couldn't open that book", true);
      const html = await res.text();
      const wasOpen = shell.classList.contains("insp-open");
      const src = animate && !wasOpen ? $("img", itemById(id) || document.createElement("i")) : null;
      if (src) src.style.viewTransitionName = "hero";
      const apply = () => {
        if (src) src.style.viewTransitionName = "";
        inspIn.innerHTML = html;
        htmx.process(inspIn);
        shell.classList.add("insp-open");
        S.open = id;
        markOpen();
        const to = $(".icv img", inspIn);
        if (src && to) to.style.viewTransitionName = "hero";
      };
      const t = animate ? vt(apply) : (apply(), null);
      t?.finished.finally(() => { const to = $(".icv img", inspIn); if (to) to.style.viewTransitionName = ""; });
      history.replaceState({}, "", withParam("book", id));
    }
    function closeInsp() {
      if (!S.open) return;
      const id = S.open, from = $(".icv img", inspIn), to = $("img", itemById(id) || document.createElement("i"));
      if (from && to) from.style.viewTransitionName = "hero";
      const t = vt(() => {
        shell.classList.remove("insp-open");
        inspIn.innerHTML = "";
        S.open = null;
        markOpen();
        if (from && to) to.style.viewTransitionName = "hero";
      });
      t ? t.finished.finally(() => { if (to) to.style.viewTransitionName = ""; }) : null;
      history.replaceState({}, "", withParam("book", ""));
    }
    function openFull(id) {
      if (!id) return;
      location.href = `/book/${encodeURIComponent(id)}`;
    }

    // Cross-page morph: name the visible cover "hero" as we leave for the book page.
    addEventListener("pageswap", (e) => {
      const dest = e.activation?.entry?.url || "";
      const m = dest.match(/\/book\/([^/?#]+)$/);
      if (!e.viewTransition || !m) return;
      const id = decodeURIComponent(m[1]);
      const img = (S.open === id && $(".icv img", inspIn)) || $("img", itemById(id) || document.createElement("i"));
      if (img) img.style.viewTransitionName = "hero";
    });
    addEventListener("pagereveal", (e) => {
      if (!e.viewTransition || !S.open) return;
      const img = $(".icv img", inspIn);
      if (!img) return;
      img.style.viewTransitionName = "hero";
      e.viewTransition.finished.finally(() => { img.style.viewTransitionName = ""; });
    });

    /* ---- bulk ---- */
    async function bulk(path, fields) {
      const body = new FormData();
      S.sel.forEach((id) => body.append("ids", id));
      Object.entries(fields).forEach(([k, v]) => body.append(k, v));
      const res = await fetch(path, { method: "POST", body });
      if (!res.ok) return toast("That didn't save", true);
      return res.json();
    }
    $("#bulk")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-bulk]");
      if (!btn) return;
      const a = btn.dataset.bulk;
      if (a === "none") { S.sel.clear(); renderSelection(); }
      else if (a === "all") { items().forEach((el) => el.dataset.id && S.sel.add(el.dataset.id)); renderSelection(); }
      else if (a === "tag") askTag(btn, async (tag) => {
        const r = await bulk("/books/bulk/tags", { tag });
        if (r) { toast(`Tagged ${r.updated} ${r.updated === 1 ? "book" : "books"} #${r.tag}`); refresh(); }
      });
      else if (a === "format") menu(btn, [["physical", "Physical"], ["ebook", "Ebook"], ["audiobook", "Audiobook"], ["", "Not owned"]], async (v) => {
        const r = await bulk("/books/bulk/format", { format: v });
        if (r) { toast(`Set ${r.updated} ${r.updated === 1 ? "book" : "books"} to ${v || "not owned"}`); refresh(); }
      });
    });

    /* ---- events ---- */
    document.body.addEventListener("htmx:afterSettle", (e) => {
      if (e.detail.target?.id === "browse" || e.target.id === "browse") { initBrowse(); closeSide(); }
    });
    // Sidebar edits change counts and badges; refresh the library quietly.
    document.body.addEventListener("htmx:afterRequest", (e) => {
      const path = e.detail.requestConfig?.path || "";
      if (e.detail.successful && /\/book\/[^/]+\/(tags\/(add|remove)|format|loaned)$/.test(path)) refresh();
    });
    addEventListener("popstate", () => { S.open = new URLSearchParams(location.search).get("book"); navigate(location.pathname + location.search, { push: false }); });

    shell.addEventListener("click", (e) => {
      const t = e.target;
      // Book sidebar controls.
      if (t.closest("#inspIn [data-close]")) return closeInsp();
      if (t.closest("#inspIn [data-more]")) { const d = $(".desc", inspIn); d?.classList.remove("clamp"); t.closest("[data-more]").remove(); return; }
      if (t.closest("#inspIn [data-expand]")) return; // real link to the full page
      // Library items.
      const it = t.closest("#content [data-i]");
      if (!it) return;
      const id = it.dataset.id, i = items().indexOf(it);
      if (!id) return;
      if (t.closest("[data-sel]") || t.matches("[data-cb]") || e.metaKey || e.ctrlKey) {
        e.preventDefault(); S.focus = i; markFocus(); return toggleSel(id);
      }
      if (e.shiftKey && S.focus >= 0) {
        e.preventDefault();
        items().slice(Math.min(S.focus, i), Math.max(S.focus, i) + 1).forEach((x) => x.dataset.id && S.sel.add(x.dataset.id));
        S.focus = i; markFocus(); return renderSelection();
      }
      if (t.closest("td.tt a")) return; // the title link goes to the full page
      e.preventDefault();
      S.focus = i; markFocus();
      S.open === id ? closeInsp() : openInsp(id);
    });
    shell.addEventListener("dblclick", (e) => { const it = e.target.closest("#content [data-id]"); if (it && !e.target.closest("[data-sel],[data-cb]")) openFull(it.dataset.id); });
    shell.addEventListener("change", (e) => {
      if (e.target.id === "selAll") { items().forEach((el) => el.dataset.id && (e.target.checked ? S.sel.add(el.dataset.id) : S.sel.delete(el.dataset.id))); renderSelection(); }
    });
    shell.addEventListener("input", (e) => {
      if (e.target.id === "size") { store.set("adso_size", e.target.value); layout(); }
      if (e.target.id === "tagfilter") { const q = e.target.value.trim().toLowerCase(); $$("#sideTags [data-tag]").forEach((a) => { a.hidden = !!q && !a.dataset.tag.includes(q); }); }
    });
    shell.addEventListener("click", (e) => {
      if (!e.target.closest("#titlesBtn")) return;
      const on = !$("#browse").classList.contains("show-titles");
      store.set("adso_titles", on ? "1" : "0");
      $("#browse").classList.toggle("show-titles", on);
      $("#titlesBtn").classList.toggle("on", on);
      layout();
    });
    $("#views")?.addEventListener("click", (e) => {
      const b = e.target.closest("[data-view]");
      if (!b || b.dataset.view === view()) return;
      vt(() => navigate(withParam("view", b.dataset.view === "grid" ? "" : b.dataset.view)));
    });
    $("#helpBtn")?.addEventListener("click", () => $("#keys")?.classList.toggle("show"));
    $("#menuBtn")?.addEventListener("click", () => { $("#side")?.classList.add("show"); $("#scrim")?.classList.add("show"); });
    $("#scrim")?.addEventListener("click", closeSide);
    function closeSide() { $("#side")?.classList.remove("show"); $("#scrim")?.classList.remove("show"); }

    // Wall: a small tooltip names the tile under the pointer.
    shell.addEventListener("mousemove", (e) => {
      const tip = $("#tip");
      if (!tip) return;
      const w = e.target.closest(".wall .w");
      if (!w) return tip.classList.remove("show");
      const r = +w.dataset.rating;
      tip.innerHTML = `<b>${esc(w.dataset.title)}</b><span>${esc(w.dataset.author)}${r ? " · " + "★".repeat(r) : ""}</span>`;
      tip.classList.add("show");
      tip.style.left = Math.min(e.clientX + 14, innerWidth - 280) + "px";
      tip.style.top = e.clientY + 20 + "px";
    });
    shell.addEventListener("mouseleave", () => $("#tip")?.classList.remove("show"));

    document.addEventListener("keydown", (e) => {
      const a = document.activeElement;
      const typing = a && ((a.tagName === "INPUT" && !["checkbox", "range"].includes(a.type)) || a.tagName === "TEXTAREA" || a.tagName === "SELECT" || a.isContentEditable);
      if (e.key === "Escape") {
        if (popEl) return closePop();
        if (typing) return a.blur();
        if ($("#keys")?.classList.contains("show")) return $("#keys").classList.remove("show");
        if (S.open) return closeInsp();
        if (S.sel.size) { S.sel.clear(); return renderSelection(); }
        return closeSide();
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey || popEl) return;
      const k = e.key, f = S.focus, list = items();
      if (k === "/") { e.preventDefault(); $("#q")?.focus(); $("#q")?.select(); }
      else if (k === "?") $("#keys")?.classList.toggle("show");
      else if (k === "1" || k === "2" || k === "3") $(`#views [data-view="${["grid", "table", "wall"][+k - 1]}"]`)?.click();
      else if (k === "j" || k === "ArrowDown") { e.preventDefault(); moveVert(1); }
      else if (k === "k" || k === "ArrowUp") { e.preventDefault(); moveVert(-1); }
      else if (k === "l" || k === "ArrowRight") { e.preventDefault(); setFocus(f + 1); }
      else if (k === "h" || k === "ArrowLeft") { e.preventDefault(); setFocus(f - 1); }
      else if ((k === "Enter" || k === "o" || k === " ") && f >= 0) { e.preventDefault(); const id = list[f]?.dataset.id; S.open === id ? closeInsp() : openInsp(id); }
      else if (k === "f") { const id = S.open || list[f]?.dataset.id; if (id) openFull(id); }
      else if (k === "x" && f >= 0) toggleSel(list[f]?.dataset.id);
    });

    // Re-flow the covers once the width settles (e.g. after the book sidebar
    // opens) so they glide into their new columns.
    let roTimer;
    const main = $("#main");
    new ResizeObserver(() => { clearTimeout(roTimer); roTimer = setTimeout(() => { layout(); syncViewSeg(); }, 90); }).observe(document.body);
    if (main) new ResizeObserver(() => { clearTimeout(roTimer); roTimer = setTimeout(() => layout(), 90); }).observe(main);

    initSearch((q) => navigate(withParam("q", q)));
    initBrowse();
    document.fonts?.ready.then(syncViewSeg);
  }

  function placeThumb(seg) {
    const on = $("button.on", seg), th = $(".thumb", seg);
    if (!th) return;
    if (!on) { th.style.width = "0"; return; }
    th.style.width = on.offsetWidth + "px";
    th.style.transform = `translateX(${on.offsetLeft}px)`;
  }

  /* ========================================================== book page */
  function initBookPage() {
    const page = $(".bookpage"), id = page.dataset.book;
    const list = session.get("adso:list");
    const back = $("[data-back]");
    // Back returns to the exact library view you came from, sidebar open.
    if (back && list?.url) {
      const u = new URL(list.url, location.origin);
      u.searchParams.set("book", id);
      back.href = u.pathname + u.search;
    }
    const idx = list?.ids?.indexOf(id) ?? -1;
    const np = $("#nextprev");
    if (np && idx >= 0) {
      const [prev, next] = [list.ids[idx - 1], list.ids[idx + 1]];
      const [pa, na] = $$("[data-step]", np);
      if (prev) pa.href = `/book/${encodeURIComponent(prev)}`; else pa.style.visibility = "hidden";
      if (next) na.href = `/book/${encodeURIComponent(next)}`; else na.style.visibility = "hidden";
      $("#npPos").textContent = `${String(idx + 1).padStart(2, "0")} / ${list.ids.length}`;
      np.hidden = false;
      document.addEventListener("keydown", (e) => {
        const a = document.activeElement;
        if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")) return;
        if ((e.key === "ArrowRight" || e.key === "l") && next) location.href = na.href;
        else if ((e.key === "ArrowLeft" || e.key === "h") && prev) location.href = pa.href;
        else if (e.key === "Escape") location.href = back.href;
      });
    }
    $$(".cv img").forEach((img) => (img.complete && img.naturalWidth ? img.classList.add("ld") : img.addEventListener("load", () => img.classList.add("ld"), { once: true })));
    // Leaving for the library: name the hero so it morphs back into the sidebar.
    addEventListener("pageswap", (e) => {
      if (!e.viewTransition) return;
      const dest = e.activation?.entry?.url || "";
      if (!new URL(dest, location.origin).pathname.startsWith("/book/")) $(".hero").style.viewTransitionName = "hero";
      else $(".hero").style.viewTransitionName = "none";
    });
    initSearch(null);
  }

  /* --------------------------------------------------------------- boot */
  function boot() {
    initTheme();
    initRotator();
    if ($("#shell")) initLibrary();
    else if ($(".bookpage")) initBookPage();
    else initSearch(null);
  }
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", boot) : boot();
})();
