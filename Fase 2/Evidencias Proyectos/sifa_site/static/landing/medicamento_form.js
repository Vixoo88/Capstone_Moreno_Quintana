// static/landing/medicamento_form.js
(function () {
  const ready = (fn) => (document.readyState !== 'loading') ? fn() : document.addEventListener('DOMContentLoaded', fn);
  ready(function () {
    const qs  = (s, r=document) => r.querySelector(s);
    const qsa = (s, r=document) => Array.from(r.querySelectorAll(s));

    // --- Toggle "Crear nuevo" / "Seleccionar existente"
    const toggle         = qs('#toggleNuevo');
    const bloqueSelect   = qs('#bloqueSelect');
    const bloqueNuevo    = qs('#bloqueNuevo');
    const selectProducto = qs('#id_producto');

    function setDisabled(container, disabled){
      if(!container) return;
      qsa('input,select,textarea', container).forEach(el=>{
        if(disabled){ el.dataset.wasRequired = el.required ? '1' : ''; el.required=false; el.disabled=true; }
        else { el.disabled=false; if(el.dataset.wasRequired === '1') el.required=true; }
      });
    }
    function applyToggle(){
      const creating = toggle && toggle.checked;
      if(bloqueSelect) bloqueSelect.classList.toggle('d-none', creating);
      if(bloqueNuevo)  bloqueNuevo.classList.toggle('d-none', !creating);
      setDisabled(bloqueSelect, creating);
      setDisabled(bloqueNuevo, !creating);
      if (creating && selectProducto) selectProducto.value = '';
    }
    if (toggle){ toggle.addEventListener('change', applyToggle); applyToggle(); }

    // --- Horas dinámicas
    const cont   = qs('#horasContainer');
    const tpl    = qs('#rowTpl');
    const btnAdd = qs('#btnAddHora');

    function wireRemove(btn){
      if(!btn) return;
      btn.addEventListener('click', (e)=>{
        const row = e.currentTarget.closest('.hora-row');
        if(row) row.remove();
      });
    }
    function addRow(){
      if(!tpl || !cont) return;
      const node = tpl.content.cloneNode(true);
      wireRemove(qs('.btnRemove', node));
      cont.appendChild(node);
    }
    if (btnAdd) btnAdd.addEventListener('click', addRow);
    qsa('.btnRemove', cont||document).forEach(wireRemove);

    // --- Autocomplete SOLO en "Crear nuevo"
    const inputNombre   = bloqueNuevo ? (qs('input[name$="nombre"]', bloqueNuevo)   || qs('#id_prod-nombre'))   : null;
    const inputPotencia = bloqueNuevo ? (qs('input[name$="potencia"]', bloqueNuevo) || qs('#id_prod-potencia')) : null;
    const inputForma    = bloqueNuevo ? (qs('input[name$="forma"]', bloqueNuevo)    || qs('#id_prod-forma'))    : null;

    const box = qs('#prod-suggest');
    const suggestUrl = box ? box.dataset.url : null;

    const debounce = (fn, t=220) => { let id; return (...args)=>{ clearTimeout(id); id=setTimeout(()=>fn(...args), t); }; };
    function clearBox(){ if(box){ box.classList.add('d-none'); box.innerHTML=''; } }

    function ensureOptionInSelect(id, label){
      if(!selectProducto) return;
      let opt = qsa('option', selectProducto).find(o => o.value === String(id));
      if(!opt){
        opt = new Option(label, String(id), true, true);
        selectProducto.add(opt);
      } else {
        opt.selected = true;
      }
    }

    function renderSugs(items){
      if(!box) return;
      clearBox();
      if(!items.length) return;

      items.forEach(it=>{
        const a = document.createElement('button');
        a.type = 'button';
        a.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-center';
        const right = it.source === 'local' ? 'Usar existente' : 'Completar';
        a.innerHTML = `<span>${it.label}</span><span class="badge bg-light text-dark">${right}</span>`;
        a.addEventListener('click', ()=>{
          if (it.source === 'local') {
            // Cambia a "Seleccionar existente" y selecciona el producto local
            if(toggle){ toggle.checked = false; applyToggle(); }
            ensureOptionInSelect(it.id, it.label);
          } else {
            // Mantiene "Crear nuevo" y completa los campos
            if(toggle){ toggle.checked = true; applyToggle(); }
            if(inputNombre)   inputNombre.value   = it.nombre || it.label || '';
            if(inputPotencia) inputPotencia.value = it.potencia || '';
            if(inputForma)    inputForma.value    = it.forma || '';
          }
          clearBox();
        });
        box.appendChild(a);
      });
      box.classList.remove('d-none');
    }

    async function fetchSugs(q){
      if(!suggestUrl) return [];
      try{
        // Puedes forzar proveedor con ?provider=CIMA|RXNORM|LOCAL|HYBRID en la query del template si quieres
        const res = await fetch(`${suggestUrl}?q=${encodeURIComponent(q)}`, {headers:{'Accept':'application/json'}});
        if(!res.ok) return [];
        const data = await res.json();
        return data.results || [];
      }catch{ return []; }
    }

    const onInput = debounce(async ()=>{
      if(!(toggle && toggle.checked)) { clearBox(); return; }         // solo en "Crear nuevo"
      const q = (inputNombre?.value || '').trim();
      if(q.length < 2){ clearBox(); return; }
      const items = await fetchSugs(q);
      renderSugs(items);
    });

    if (inputNombre){
      inputNombre.setAttribute('autocomplete','off');
      inputNombre.addEventListener('input', onInput);
      inputNombre.addEventListener('focus', onInput);
      document.addEventListener('click', (e)=>{ if(box && !box.contains(e.target) && e.target !== inputNombre){ clearBox(); }});
    }
  });
})();
