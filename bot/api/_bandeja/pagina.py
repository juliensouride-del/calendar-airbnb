"""Pantalla de edición (Mini App de Telegram): se abre con ✏️ Editar en la tarjeta del borrador."""

HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Editar borrador</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 14px 14px 90px; font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #111); }
  .cab { font-weight: 600; font-size: 16px; }
  .sub { color: var(--tg-theme-hint-color, #888); font-size: 13px; margin-top: 2px; }
  details { margin: 12px 0; background: var(--tg-theme-secondary-bg-color, #f3f3f3); border-radius: 10px; padding: 8px 10px; }
  summary { cursor: pointer; color: var(--tg-theme-hint-color, #777); font-size: 13px; }
  .huesped { white-space: pre-wrap; margin-top: 6px; }
  .aviso { border-radius: 10px; padding: 8px 10px; margin: 10px 0; font-size: 13px;
           background: color-mix(in srgb, #f5a623 18%, transparent); }
  .aviso.prueba { background: color-mix(in srgb, #7b61ff 18%, transparent); }
  textarea { width: 100%; min-height: 46vh; padding: 10px; border-radius: 10px; font: inherit; resize: vertical;
             border: 1px solid var(--tg-theme-hint-color, #ccc); background: var(--tg-theme-bg-color, #fff);
             color: var(--tg-theme-text-color, #111); }
  .fila { display: flex; gap: 8px; margin-top: 10px; }
  button { flex: 1; padding: 11px; border: 0; border-radius: 10px; font: inherit; font-weight: 600;
           background: var(--tg-theme-secondary-bg-color, #eee); color: var(--tg-theme-text-color, #111); }
  .cont { color: var(--tg-theme-hint-color, #888); font-size: 12px; text-align: right; margin-top: 4px; }
  ul { margin: 6px 0 0; padding-left: 18px; }
  #error { color: #d33; margin-top: 10px; }
</style>
</head>
<body>
<div id="app"><div class="sub">Cargando…</div></div>
<script>
const tg = window.Telegram.WebApp;
tg.ready(); tg.expand();
const id = new URLSearchParams(location.search).get("id");
const api = (ruta, opciones = {}) => fetch(ruta, {...opciones, headers: {"Content-Type": "application/json",
  "X-Init-Data": tg.initData, ...(opciones.headers || {})}}).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ("Error " + r.status));
    return d;
  });
const esc = s => (s || "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const fecha = d => d ? d.slice(8, 10) + "/" + d.slice(5, 7) : "?";
const huecos = t => /\[\[\s*COMPLETAR/i.test(t);
let b = null, ocupado = false;

function pintar() {
  const app = document.getElementById("app");
  if (b.estado !== "pendiente" && b.estado !== "error") {
    app.innerHTML = `<div class="cab">Este borrador ya no está pendiente (${esc(b.estado)}).</div>`;
    tg.MainButton.hide(); return;
  }
  app.innerHTML = `
    <div class="cab">${esc(b.piso)} · ${esc(b.huesped)}</div>
    <div class="sub">${fecha(b.checkin)} → ${fecha(b.checkout)} · ${esc(b.personas || "")} · ${esc((b.idioma || "").toUpperCase())}</div>
    ${b.pruebas ? '<div class="aviso prueba">🧪 PRUEBAS: al «enviar» no le llega nada al huésped.</div>' : ""}
    ${b.mensaje_huesped ? `<details><summary>Mensaje del huésped</summary><div class="huesped">${esc(b.mensaje_huesped)}</div></details>` : ""}
    ${(b.faltan || []).length ? `<div class="aviso">⚠️ Falta completar:<ul>${b.faltan.map(f => `<li>${esc(f)}</li>`).join("")}</ul></div>` : ""}
    ${b.se_envia ? "" : '<div class="aviso">📋 Este no se puede enviar desde aquí: guárdalo, cópialo de la tarjeta y pégalo en Airbnb.</div>'}
    <textarea id="t" spellcheck="true"></textarea>
    <div class="cont" id="cont"></div>
    <div class="fila"><button id="guardar">Guardar sin enviar</button><button id="original">Volver al original</button></div>
    <div id="error"></div>`;
  const t = document.getElementById("t");
  const guardado = sessionStorage.getItem("b_" + id);
  t.value = guardado !== null ? guardado : b.texto;
  t.addEventListener("input", actualizar);
  document.getElementById("guardar").onclick = guardar;
  document.getElementById("original").onclick = () => { t.value = b.texto_original; actualizar(); };
  actualizar();
}

function actualizar() {
  const t = document.getElementById("t");
  try { sessionStorage.setItem("b_" + id, t.value); } catch (e) {}
  document.getElementById("cont").textContent = t.value.length + " caracteres";
  const bloqueado = huecos(t.value);
  if (!b.se_envia) { tg.MainButton.setParams({text: "Guardar", is_visible: true, is_active: true}); return; }
  tg.MainButton.setParams({text: bloqueado ? "Rellena los [[COMPLETAR]]" : "Enviar al huésped",
                           is_visible: true, is_active: !bloqueado});
}

function fallo(e) {
  ocupado = false; tg.MainButton.hideProgress();
  document.getElementById("error").textContent = e.message;
  tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("error");
}

function guardar() {
  if (ocupado) return; ocupado = true;
  const texto = document.getElementById("t").value;
  api(`/api/b/${id}/guardar`, {method: "POST", body: JSON.stringify({texto})}).then(() => {
    try { sessionStorage.removeItem("b_" + id); } catch (e) {}
    tg.close();
  }).catch(fallo);
}

function enviar() {
  if (!b.se_envia) return guardar();
  const texto = document.getElementById("t").value;
  if (ocupado || huecos(texto)) return;
  const nombre = (b.huesped || "el huésped").split(" ")[0];
  tg.showConfirm(`¿Enviar este mensaje a ${nombre}?`, ok => {
    if (!ok) return;
    ocupado = true; tg.MainButton.showProgress();
    api(`/api/b/${id}/enviar`, {method: "POST", body: JSON.stringify({texto})}).then(r => {
      try { sessionStorage.removeItem("b_" + id); } catch (e) {}
      tg.HapticFeedback && tg.HapticFeedback.notificationOccurred("success");
      tg.showAlert(r.mensaje || "Enviado", () => tg.close());
    }).catch(fallo);
  });
}

tg.MainButton.onClick(enviar);
if (!id) { document.getElementById("app").textContent = "Falta el borrador."; }
else api(`/api/b/${id}`).then(d => { b = d; pintar(); }).catch(e => {
  document.getElementById("app").innerHTML = `<div id="error">${esc(e.message)}</div>`;
});
</script>
</body>
</html>
"""
