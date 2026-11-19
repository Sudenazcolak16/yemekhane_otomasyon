function kullaniciGetir() {
  const h = localStorage.getItem("kullanici");
  return h ? JSON.parse(h) : null;
}

function oturumKontrol(y = false) {
  const k = kullaniciGetir();
  if (!k || !k.token) {
    localStorage.removeItem("kullanici");
    location.href = "/index.html";
    return null;
  }
  if (y && k.rol !== "yonetici") {
    location.href = "/menuler.html";
    return null;
  }
  return k;
}

async function cikisYap() {
  const k = kullaniciGetir();
  if (k && k.token) {
    try {
      await fetch("/api/cikis", {
        method: "POST",
        headers: { Authorization: "Bearer " + k.token },
      });
    } catch (e) {}
  }
  localStorage.removeItem("kullanici");
  location.href = "/index.html";
}

function ustMenuCiz(a) {
  const k = kullaniciGetir();
  if (!k) return;
  const l = [
    ["menuler.html", "Menuler"],
    ["rezervasyonlar.html", "Rezervasyonlarim"],
    ["bakiye.html", "Bakiye"],
    ["profil.html", "Profil"],
    ["turnike.html", "Turnike Testi"],
  ];
  if (k.rol === "yonetici") l.push(["yonetici.html", "Yonetici Paneli"]);
  const n = l
    .map(([u, ad]) => `<a href="/${u}" class="${a === u ? "aktif" : ""}">${ad}</a>`)
    .join("");
  document.getElementById("ust-bar").innerHTML =
    `<div class="ust-menu"><div><strong>Yemekhane</strong> | ${k.ad} ${k.soyad} (<span id="ust-bakiye">${Number(k.bakiye).toFixed(2)}</span> TL)</div><nav>${n}<button type="button" class="dugme" onclick="cikisYap()">Cikis</button></nav></div>`;
}

async function apiIstek(u, s = {}) {
  const k = kullaniciGetir();
  const h = { "Content-Type": "application/json", ...(s.headers || {}) };
  if (k && k.token) h["Authorization"] = "Bearer " + k.token;
  const r = await fetch(u, { ...s, headers: h });
  const v = await r.json().catch(() => ({}));
  if (r.status === 401 && u !== "/api/giris") {
    localStorage.removeItem("kullanici");
    location.href = "/index.html";
    throw new Error(v.detail || "Oturum süresi doldu.");
  }
  if (!r.ok) throw new Error(v.detail || "Hata olustu");
  return v;
}

async function bakiyeYenile() {
  const mevcut = kullaniciGetir();
  try {
    const p = await apiIstek("/api/profil");
    const guncel = { ...p, token: mevcut ? mevcut.token : undefined };
    localStorage.setItem("kullanici", JSON.stringify(guncel));
    const e = document.getElementById("ust-bakiye");
    if (e) e.textContent = Number(p.bakiye).toFixed(2);
    return guncel;
  } catch (e) {
    return mevcut;
  }
}

function mesajGoster(id, m, h = false) {
  const k = document.getElementById(id);
  if (!k) return;
  k.className = "bildirim " + (h ? "bildirim-hata" : "bildirim-basari");
  k.textContent = m;
  k.classList.remove("gizli");
}