// static/landing/producto_suggest.js
(function(){
  const input = document.getElementById("id_nombre");
  const list = document.getElementById("prod-suggest");
  if(!input || !list) return;

  let t = null;

  function hide() { list.classList.add("d-none"); list.innerHTML = ""; }
  function show() { list.classList.remove("d-none"); }

  async function fetchSuggest(q){
    const url = list.dataset.url;
    try{
      const res = await fetch(`${url}?q=${encodeURIComponent(q)}&provider=HYBRID`, {
        headers: { "Accept": "application/json" }
      });
      if(!res.ok) return [];
      const data = await res.json();
      return data.results || [];
    }catch(_){ return []; }
  }

  function render(items){
    if(!items.length){ hide(); return; }
    list.innerHTML = items.map(it => {
      const label = it.label || it.nombre || "";
      const forma = it.forma || "";
      const potencia = it.potencia || "";
      const source = it.source || "local";
      return `
        <button type="button"
          class="list-group-item list-group-item-action d-flex justify-content-between align-items-center"
          data-nombre="${label.replace(/"/g,'&quot;')}"
          data-potencia="${potencia.replace(/"/g,'&quot;')}"
          data-forma="${forma.replace(/"/g,'&quot;')}">
          <span>${label}${potencia ? " · " + potencia : ""}${forma ? " — " + forma : ""}</span>
          <small class="text-muted text-uppercase">${source}</small>
        </button>`;
    }).join("");
    show();
  }

  list.addEventListener("click", (ev)=>{
    const btn = ev.target.closest("button[data-nombre]");
    if(!btn) return;
    document.getElementById("id_nombre").value = btn.dataset.nombre || "";
    const pot = document.getElementById("id_potencia");
    const frm = document.getElementById("id_forma");
    if(pot) pot.value = btn.dataset.potencia || "";
    if(frm) frm.value = btn.dataset.forma || "";
    hide();
    input.focus();
  });

  input.addEventListener("input", ()=>{
    const q = (input.value || "").trim();
    if(t) clearTimeout(t);
    if(q.length < 2){ hide(); return; }
    t = setTimeout(async ()=>{
      const items = await fetchSuggest(q);
      render(items);
    }, 220);
  });

  input.addEventListener("blur", ()=> setTimeout(hide, 180));
  input.setAttribute("autocomplete", "off");
})();
