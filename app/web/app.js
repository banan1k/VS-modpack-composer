const state = {
  mods: [],
  selected: new Set(),
  pendingBuild: null,
  buildName: '',
  gameVersion: '',
  search: ''
};
const $ = (s) => document.querySelector(s);

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, ch => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[ch]));
}

function filteredMods() {
  const q = state.search.trim().toLocaleLowerCase('ru-RU');
  if (!q) return state.mods;
  return state.mods.filter(m => [m.name, m.mod_id, m.mod_db_id, m.mod_db_asset_id, m.short_description, m.description]
    .some(v => String(v ?? '').toLocaleLowerCase('ru-RU').includes(q)));
}

async function loadCatalog() {
  const res = await fetch('/api/catalog');
  if (!res.ok) return;
  state.mods = await res.json();
  render();

  const match = location.pathname.match(/^\/build\/([^/]+)/);
  if (match) {
    try {
      const b = await (await fetch(`/api/builds/${match[1]}`)).json();
      state.selected = new Set(b.mods.map(m => m.id));
      state.gameVersion = b.target_game_version || '';
      state.buildName = b.name || '';
      $('#gameVersion').value = state.gameVersion;
      render();
    } catch (_) {}
  }
}

function render() {
  $('#catalog').innerHTML = filteredMods().map(card).join('') || '<div class="empty-state">Моды не найдены.</div>';
  $('#selectedCount').textContent = state.selected.size;
  $('#selectedCountSmall').textContent = state.selected.size;
  $('#createBtn').disabled = state.selected.size === 0;
  const visible = filteredMods().length;
  $('#catalogMeta').textContent = state.search.trim()
    ? `${visible} из ${state.mods.length} модов`
    : `${state.mods.length} модов в локальной базе`;
  renderSelectionList();
}

function card(m) {
  const selected = state.selected.has(m.id);
  const version = m.latest_game_version || 'Версия не указана';
  const displayId = m.display_mod_id || m.mod_id || m.mod_db_id || m.mod_db_asset_id;
  const modId = displayId ? `modid: ${displayId}` : 'modid: не указан';
  const modIdHtml = m.mod_db_url
    ? `<a class="modid" href="${esc(m.mod_db_url)}" target="_blank" rel="noopener">${esc(modId)}</a>`
    : `<span class="modid">${esc(modId)}</span>`;

  return `<article class="card ${selected ? 'is-selected' : ''}">
    ${m.image_url ? `<img class="cover" loading="lazy" src="${esc(m.image_url)}" alt="" />` : `<div class="cover empty">Нет превью</div>`}
    <div class="card-body">
      <div class="title-row"><div class="title">${esc(m.name)}</div><span class="badge">${esc(version)}</span></div>
      <div class="desc">${esc(m.short_description || m.description || 'Описание отсутствует')}</div>
      <div>${modIdHtml}</div>
      <div class="actions">
        <button class="select-btn ${selected ? 'selected' : ''}" type="button" data-select="${m.id}" aria-pressed="${selected}">${selected ? 'Выбрано' : 'Выбрать'}</button>
        <button type="button" data-rel="${m.id}">Связи</button>
      </div>
    </div>
    <div id="rel-${m.id}" class="rel-panel"></div>
  </article>`;
}

function renderSelectionList() {
  const selectedMods = state.mods.filter(m => state.selected.has(m.id));
  $('#selectionList').innerHTML = selectedMods.length
    ? `${selectedMods.map(m => `<div class="selection-row">
        <a href="${esc(m.mod_db_url || '#')}" target="_blank" rel="noopener" class="selection-name">${esc(m.name)}</a>
        <button type="button" class="remove-selection" data-remove="${m.id}" aria-label="Убрать ${esc(m.name)}">×</button>
      </div>`).join('')}<button id="clearSelection" class="clear-selection" type="button">Очистить всё</button>`
    : '<div class="selection-empty">Ничего не выбрано</div>';
}

function relationLabel(type) {
  return ({
    dependency: 'зависимость',
    optional_dependency: 'опциональная зависимость',
    compatible: 'совместим',
    incompatible: 'конфликт',
    addon: 'дополнение',
    required_by: 'нужен другим модам'
  })[type] || type;
}

function relationHtml(d) {
  const all = [ ...(d.dependencies || []), ...(d.compatibility || []), ...(d.addons || []) ];
  if (!all.length) return '<div class="relation-empty">Связи не найдены</div>';
  return all.map(x => {
    const label = relationLabel(x.relation_type);
    const confidence = `${Math.round((x.confidence ?? 0) * 100)}%`;
    const name = x.target_url
      ? `<a href="${esc(x.target_url)}" target="_blank" rel="noopener">${esc(x.target_name || 'Без названия')}</a>`
      : `<span>${esc(x.target_name || 'Не сопоставлено')}</span>`;
    const lowConfidence = (x.confidence ?? 0) < 0.90;
    const confidenceNote = lowConfidence ? '<em class="rel-note">не учитывается при проверке сборки</em>' : '';
    return `<div class="rel-line">
      <div class="rel-main">${name}<small>${esc(x.raw_phrase || '')}</small>${confidenceNote}</div>
      <div class="rel-meta"><strong class="rel-type ${esc(x.relation_type)}">${esc(label)}</strong><span>${esc(confidence)}</span></div>
    </div>`;
  }).join('');
}

async function toggleRelation(id, button) {
  const p = document.querySelector(`#rel-${id}`);
  if (p.classList.contains('open')) { p.classList.remove('open'); return; }
  button.disabled = true;
  try {
    const res = await fetch(`/api/mods/${id}/relations`);
    const data = await res.json();
    p.innerHTML = relationHtml(data);
    p.classList.add('open');
  } catch (err) {
    p.innerHTML = `<div class="relation-empty">Ошибка загрузки связей: ${esc(err.message || err)}</div>`;
    p.classList.add('open');
  } finally {
    button.disabled = false;
  }
}

function showProgress(title, text = '') {
  $('#progressTitle').textContent = title;
  $('#progressText').textContent = text;
  $('#progressCount').textContent = '—';
  $('#progressBar').style.width = '0%';
  $('#progress').classList.remove('hidden');
}

async function pollCatalog(id) {
  const j = await (await fetch(`/api/jobs/${id}`)).json();
  $('#progressTitle').textContent = 'Обновление каталога';
  $('#progressText').textContent = j.message || '';
  $('#progressCount').textContent = j.total ? `${j.current}/${j.total}` : '—';
  $('#progressBar').style.width = (j.total ? Math.min(100, Math.round(j.current / j.total * 100)) : 0) + '%';
  if (j.status === 'done') {
    $('#refreshBtn').disabled = false;
    await loadCatalog();
    setTimeout(() => $('#progress').classList.add('hidden'), 1400);
  } else if (j.status === 'failed') {
    $('#refreshBtn').disabled = false;
    alert(j.message || 'Ошибка обновления каталога');
    showProgress('Ошибка обновления', j.message || '');
  } else {
    setTimeout(() => pollCatalog(id), 500);
  }
}

async function startSync() {
  $('#refreshBtn').disabled = true;
  showProgress('Обновление каталога', 'Подключение к Discord…');
  const res = await fetch('/api/sync', { method: 'POST' });
  if (!res.ok) {
    let body = {}; try { body = await res.json(); } catch (_) {}
    $('#refreshBtn').disabled = false;
    alert(body.detail || 'Не удалось запустить обновление');
    return;
  }
  const { job_id } = await res.json();
  pollCatalog(job_id);
}

function openBuildDialog() {
  $('#buildName').value = state.buildName || '';
  $('#gameVersion').value = state.gameVersion || '';
  $('#buildDialog').showModal();
}

async function inspectAndCreate() {
  state.buildName = $('#buildName').value.trim() || 'Vintage Story Modpack';
  state.gameVersion = $('#gameVersion').value.trim();
  state.pendingBuild = {
    mod_ids: [...state.selected],
    target_game_version: state.gameVersion || null,
    name: state.buildName
  };
  $('#buildDialog').close();
  showProgress('Создание сборки', 'Проверка зависимостей и версий…');
  const res = await fetch('/api/builds/inspect', {
    method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(state.pendingBuild)
  });
  const data = await res.json();
  if (data.issues?.length) {
    $('#issuesBody').innerHTML = data.issues.map(i => `<div class="issue ${esc(i.severity)}"><strong>${esc(i.mod_name)}</strong><div>${esc(i.message)}</div></div>`).join('');
    $('#issuesDialog').showModal();
    $('#progress').classList.add('hidden');
    return;
  }
  await createBuild(false);
}

async function createBuild(force) {
  const payload = {...state.pendingBuild, force};
  const res = await fetch('/api/builds', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(payload)});
  const data = await res.json();
  if (!res.ok) { alert(data.detail || 'Не удалось создать сборку'); return; }
  state.pendingBuild.share_id = data.share_id;
  const target = state.gameVersion ? `Vintage Story ${esc(state.gameVersion)}` : 'Версия Vintage Story не указана';
  $('#shareText').innerHTML = `<strong>${esc(data.name)}</strong><br>${target}<br>Share ID: <code>${esc(data.share_id)}</code><br><a href="${esc(data.url)}">${esc(data.url)}</a>`;
  $('#downloadStep').classList.add('hidden');
  $('#publishBtn').disabled = false;
  $('#noPublish').disabled = false;
  $('#progress').classList.add('hidden');
  $('#publishDialog').showModal();
}

async function doPublish() {
  const res = await fetch(`/api/builds/${state.pendingBuild.share_id}/publish`, {
    method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({publish:true})
  });
  const d = await res.json();
  if (!res.ok) { alert(d.detail || 'Ошибка публикации'); return; }
  $('#publishBtn').disabled = true;
  $('#noPublish').disabled = true;
  $('#shareText').innerHTML += '<br><span class="ok-text">Опубликовано в Discord.</span>';
  $('#downloadStep').classList.remove('hidden');
}

async function beginDownload() {
  const r = await fetch(`/api/builds/${state.pendingBuild.share_id}/prepare`, {method:'POST'});
  const d = await r.json();
  if (!r.ok) { alert(d.detail || 'Не удалось начать подготовку'); return; }
  $('#publishDialog').close();
  showProgress('Подготовка ZIP', 'Загрузка модов…');
  pollDownload(d.job_id);
}

async function pollDownload(id) {
  const j = await (await fetch(`/api/jobs/${id}`)).json();
  $('#progressTitle').textContent = 'Подготовка ZIP';
  $('#progressText').textContent = j.message || '';
  $('#progressCount').textContent = j.total ? `${j.current}/${j.total}` : '—';
  $('#progressBar').style.width = (j.total ? Math.min(100, Math.round(j.current / j.total * 100)) : 0) + '%';
  if (j.status === 'done') {
    $('#progressText').textContent = 'Готово. Начинаем скачивание…';
    window.location = `/api/builds/${state.pendingBuild.share_id}/download`;
  } else if (j.status === 'failed') {
    alert(j.message || 'Ошибка подготовки ZIP');
  } else {
    setTimeout(() => pollDownload(id), 400);
  }
}

document.addEventListener('click', async (e) => {
  const selectBtn = e.target.closest('[data-select]');
  if (selectBtn) {
    const id = Number(selectBtn.dataset.select);
    state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
    render();
    return;
  }
  const relBtn = e.target.closest('[data-rel]');
  if (relBtn) {
    await toggleRelation(Number(relBtn.dataset.rel), relBtn);
    return;
  }
  const removeBtn = e.target.closest('[data-remove]');
  if (removeBtn) {
    state.selected.delete(Number(removeBtn.dataset.remove));
    render();
    return;
  }
  if (e.target.closest('#clearSelection')) {
    state.selected.clear();
    render();
  }
});

$('#selectionToggle').onclick = () => {
  const panel = $('#selectionPanel');
  const collapsed = panel.classList.toggle('collapsed');
  $('#selectionToggle').setAttribute('aria-expanded', String(!collapsed));
};
$('#searchInput').addEventListener('input', e => { state.search = e.target.value; render(); });
$('#refreshBtn').onclick = startSync;
$('#createBtn').onclick = openBuildDialog;
$('#buildBack').onclick = () => $('#buildDialog').close();
$('#buildConfirm').onclick = inspectAndCreate;
$('#issuesBack').onclick = () => $('#issuesDialog').close();
$('#issuesForce').onclick = () => { $('#issuesDialog').close(); createBuild(true); };
$('#publishBtn').onclick = doPublish;
$('#noPublish').onclick = () => { $('#publishBtn').disabled = true; $('#noPublish').disabled = true; $('#downloadStep').classList.remove('hidden'); };
$('#downloadBtn').onclick = beginDownload;

loadCatalog();
