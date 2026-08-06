async function getCatalog() {
  const res = await fetch("/data/catalog.json");
  if (!res.ok) throw new Error("catalog missing");
  return res.json();
}

async function getHotel(id) {
  const res = await fetch(`/data/hotels/${id}.json`);
  if (!res.ok) throw new Error("hotel missing");
  return res.json();
}

function bg(url) {
  return url ? `background-image:url('${url}')` : "";
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function bootIndex() {
  const list = document.getElementById("list");
  const empty = document.getElementById("empty");
  try {
    const catalog = await getCatalog();
    if (!catalog.length) {
      empty.hidden = false;
      return;
    }
    list.innerHTML = catalog
      .map(
        (h) => `
      <a class="hotel-card" href="/hotel.html?id=${encodeURIComponent(h.hotel_id)}">
        <div class="cover" style="${bg(h.cover)}"></div>
        <div class="body">
          <h3>${esc(h.name || h.hotel_id)}</h3>
          <div class="meta">
            ${h.score != null ? `<span class="score">${esc(h.score)}</span>` : ""}
            <div style="margin-top:8px">${esc(h.address || "地址待补全")}</div>
            <div style="margin-top:6px">房型 ${esc(h.room_count ?? 0)} 个</div>
            <div style="margin-top:6px;font-weight:700;color:var(--accent,#ff5500)">${priceLabel(h)}</div>
          </div>
        </div>
      </a>`
      )
      .join("");
  } catch (e) {
    empty.hidden = false;
    empty.textContent = "读取数据失败。请先 crawl，再用 preview 启动。";
  }
}

function renderNearby(nearby) {
  const blocks = [];
  for (const [k, label] of [
    ["metro", "地铁"],
    ["airport", "机场"],
    ["train", "火车站"],
  ]) {
    const arr = (nearby && nearby[k]) || [];
    if (!arr.length) continue;
    blocks.push(
      `<div style="margin-bottom:8px"><strong>${label}</strong><br>${arr
        .slice(0, 4)
        .map((x) => `${esc(x.name)} ${esc(x.distance || "")}`)
        .join("<br>")}</div>`
    );
  }
  return blocks.join("") || "待获取";
}

function roomCard(room, idx) {
  const imgs = room.images || [];
  const main = imgs[0];
  const t1 = imgs[1];
  const t2 = imgs[2];
  // F-type style: show count badge on last thumb, do NOT use a fake "view all" image tile
  const countBadge =
    imgs.length > 3
      ? `<span class="count">${imgs.length}</span>`
      : imgs.length
        ? `<span class="count">${imgs.length}</span>`
        : "";

  // Prefer international plans (HKD→CNY); fall back to domestic offers.
  const plans = room.prices || [];
  const offerRows = plans.length
    ? plans.map((p) => {
        const fold = p.folded ? `<span class="pill">折叠</span>` : "";
        const left =
          p.left != null ? `<div class="left">仅剩${esc(p.left)}间</div>` : "";
        return `<div class="offer">
        <div>
          <span class="pill">${esc(p.meal || p.summary || "-")}</span>
          <span class="pill ${/免费取消|可取消|免費取消/.test(p.cancel || "") ? "good" : ""}">${esc(p.cancel || "-")}</span>
          <span class="pill">${esc(p.confirm || "")}</span>
          ${fold}
        </div>
        <div>${p.occupancy != null ? `可住 ${esc(p.occupancy)} 人` : (p.summary ? esc(p.summary) : "-")}</div>
        <div>${planPriceHtml(p)}${left}<button class="btn" type="button">查看政策</button></div>
      </div>`;
      })
    : (room.offers || []).map((o) => {
        const left =
          o.left != null ? `<div class="left">仅剩${esc(o.left)}间</div>` : "";
        return `<div class="offer">
        <div>
          <span class="pill">${esc(o.meal || "-")}</span>
          <span class="pill ${/免费取消|可取消/.test(o.cancel || "") ? "good" : ""}">${esc(o.cancel || "-")}</span>
          <span class="pill">${esc(o.confirm || "")}</span>
          <span class="pill">${esc(o.pay || "")}</span>
        </div>
        <div>${o.occupancy != null ? `可住 ${esc(o.occupancy)} 人` : "-"}</div>
        <div>${planPriceHtml(o)}${left}<button class="btn" type="button">查看政策</button></div>
      </div>`;
      });

  return `<article class="room-block" data-idx="${idx}">
    <div class="room-media">
      <div class="big" style="${bg(main)}"></div>
      <div class="thumbs">
        <div class="t" style="${bg(t1)}"></div>
        <div class="t" style="${bg(t2)}">${countBadge}</div>
      </div>
    </div>
    <div>
      <h3 class="room-name">${esc(room.room_name)}</h3>
      <div class="specs">
        <span>${esc(room.bed || "")}</span>
        <span>${esc(room.window || "")}</span>
        <span>${esc(room.smoke || "")}</span>
        <span>${esc([room.area, room.floor].filter(Boolean).join(" | "))}</span>
        <span>${esc(room.wifi || "")}</span>
      </div>
      <button class="linkish" type="button" data-open="${idx}">房间详情</button>
      <div class="offers">${offerRows.join("") || '<div class="meta" style="padding-top:10px">待获取价格</div>'}</div>
    </div>
  </article>`;
}

function priceLabel(h) {
  const minCny = h.min_price_cny;
  const minHkd = h.min_price_hkd;
  if (minCny != null) {
    const hkd =
      minHkd != null ? ` <span class="dim">(HK$${esc(fmt(minHkd))})</span>` : "";
    return `¥${esc(fmt(minCny))} 起${hkd}`;
  }
  if (minHkd != null) return `HK$${esc(fmt(minHkd))} 起`;
  return "待获取";
}

function priceInfoLine(h) {
  const pi = h.price_info;
  const label = priceLabel(h);
  let s = ` · <span style="font-weight:700;color:var(--accent,#ff5500)">${label}</span>`;
  if (pi && pi.exchange_rate) {
    s += ` <span class="dim">(HKD→CNY ${esc(pi.exchange_rate)})</span>`;
  }
  return s;
}

function fmt(n) {
  if (n == null) return "";
  return Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function planPriceHtml(p) {
  const cny = p.price_cny != null ? p.price_cny : null;
  const hkd = p.price_hkd != null ? p.price_hkd : null;
  if (cny != null) {
    const hkdBit =
      hkd != null ? ` <span class="dim">HK$${esc(fmt(hkd))}</span>` : "";
    return `<span style="font-weight:700;color:var(--accent,#ff5500)">¥${esc(fmt(cny))}${hkdBit}</span>`;
  }
  if (hkd != null) {
    return `<span style="font-weight:700;color:var(--accent,#ff5500)">HK$${esc(fmt(hkd))}</span>`;
  }
  return `<span class="dim">待获取</span>`;
}

function openModal(room) {
  const modal = document.getElementById("modal");
  document.getElementById("modalTitle").textContent = room.room_name || "";
  document.getElementById("modalExtra").textContent = [
    room.bed,
    room.window,
    room.smoke,
    room.area,
    room.floor,
  ]
    .filter(Boolean)
    .join(" · ");

  const imgs = (room.images || []).slice(0, 4);
  document.getElementById("modalGallery").innerHTML = imgs
    .map((u) => `<div class="g" style="${bg(u)}"></div>`)
    .join("");

  const cats = room.detail_categories || [];
  document.getElementById("modalCats").innerHTML = cats
    .map((c) => {
      const items = (c.items || [])
        .map((it) => {
          const free = it.free ? `<span class="tag-free">免费</span>` : "";
          const note = it.note ? `<span class="note">${esc(it.note)}</span>` : "";
          const strike = it.available === false ? ' style="opacity:.45;text-decoration:line-through"' : "";
          return `<li${strike}>✓ ${esc(it.name)}${free}${note}</li>`;
        })
        .join("");
      return `<div class="cat"><h4>${esc(c.title)}</h4><ul>${items}</ul></div>`;
    })
    .join("");

  modal.classList.add("show");
}

async function bootHotel() {
  const id = new URLSearchParams(location.search).get("id");
  if (!id) {
    location.href = "/";
    return;
  }
  const doc = await getHotel(id);
  const h = doc.hotel || {};
  const rooms = doc.rooms || [];
  window.__ROOMS__ = rooms;

  document.getElementById("title").textContent = h.name || id;
  document.getElementById("sub").textContent = `${doc.check_in || ""} ~ ${doc.check_out || ""} · source=${doc.source || "-"}${priceInfoLine(h)}`;
  document.getElementById("address").textContent = h.address || "";
  document.getElementById("stars").textContent = h.star ? "◆".repeat(Number(h.star) || 0) : "";
  document.getElementById("score").textContent = h.score ?? "-";
  document.getElementById("scoreLabel").textContent = h.score_label || "";
  document.getElementById("review").textContent = h.review_count != null ? `共 ${h.review_count} 条点评` : "";
  document.getElementById("intro").textContent = h.introduction || "待获取";
  document.getElementById("nearby").innerHTML = renderNearby(h.nearby);

  document.getElementById("features").innerHTML = (h.features || [])
    .map((f) => `<span class="chip">${esc(f.name || f)}</span>`)
    .join("");
  document.getElementById("facilities").innerHTML = (h.facilities || [])
    .map((f) => `<div class="fac-item">${esc(f.name || f)}${f.tag ? `<div class="tag-free">${esc(f.tag)}</div>` : ""}</div>`)
    .join("") || '<div class="meta">待获取</div>';

  const himgs = h.images || [];
  const hero = document.getElementById("hero");
  hero.innerHTML = `
    <div class="hero-main" style="${bg(himgs[0])}"></div>
    <div class="hero-side">
      ${[1, 2, 3, 4]
        .map((i) => {
          if (i === 4 && (h.image_count || himgs.length) > 4) {
            return `<div class="tile" style="${bg(himgs[i])}"><div class="more">查看所有${esc(h.image_count || himgs.length)}张照片</div></div>`;
          }
          return `<div class="tile" style="${bg(himgs[i])}"></div>`;
        })
        .join("")}
    </div>`;

  document.getElementById("rooms").innerHTML = rooms.map(roomCard).join("") || '<div class="empty">无房型</div>';

  document.getElementById("rooms").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-open]");
    if (!btn) return;
    const idx = Number(btn.getAttribute("data-open"));
    openModal(rooms[idx]);
  });
  const close = () => document.getElementById("modal").classList.remove("show");
  document.getElementById("closeModal").onclick = close;
  document.getElementById("closeModal2").onclick = close;
  document.getElementById("modal").addEventListener("click", (e) => {
    if (e.target.id === "modal") close();
  });
}
