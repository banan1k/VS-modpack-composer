const state = {
  mods: [],
  explicitSelected: new Set(),
  pendingBuild: null,
  buildName: '',
  gameVersion: '',
  search: '',
  selectedTags: new Set(),
  sortBy: 'name',
  sortDirection: 'asc',
  prefetchWasRunning: false,
  prefetchCatalogRefreshed: false,
  prefetchStatusText: ''
};
const $ = (s) => document.querySelector(s);

// Theme colors are intentionally defined by the website, not by Discord tag colors.
const TAG_STYLES = {
  survival:   ['#4a2027', '#ffabb4', '#8f424e'],
  crafting:   ['#4b2d1c', '#ffc08a', '#985c31'],
  building:   ['#3d2d24', '#d9b39a', '#805b44'],
  exploration:['#18374c', '#9bd8ff', '#397da5'],
  nature:     ['#183b2c', '#9be0b7', '#367d56'],
  combat:     ['#242830', '#e0e4eb', '#4c535f'],
  cosmetic:   ['#453b17', '#ffe68a', '#97802c'],
  visual:     ['#47243a', '#ffb6dc', '#98517e'],
  qol:        ['#303740', '#d0d7df', '#64707d'],
  lib:        ['#38294c', '#d5b8ff', '#7955a5']
};
const UNKNOWN_TAG_STYLES = [
  ['#173b3a', '#9de6dc', '#3e817b'],
  ['#45301d', '#f2c889', '#9e7137'],
  ['#26364e', '#b4caff', '#5b79b0'],
  ['#402b2b', '#f4b9aa', '#9e6357'],
  ['#303044', '#c7c8ff', '#7778a6']
];
const SORT_LABELS = { name: 'Название', tags: 'Теги', version: 'Версия мода' };

function tagStyle(name) {
  const key = String(name || '').trim().toLocaleLowerCase('en-US');
  let style = TAG_STYLES[key];
  if (!style) {
    let hash = 0;
    for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) | 0;
    style = UNKNOWN_TAG_STYLES[Math.abs(hash) % UNKNOWN_TAG_STYLES.length];
  }
  return `--tag-bg:${style[0]};--tag-color:${style[1]};--tag-border:${style[2]}`;
}

function tagChip(tag) {
  return `<span class="tag-chip" style="${tagStyle(tag)}">${esc(tag)}</span>`;
}

function uniqueCatalogTags() {
  return [...new Set(state.mods.flatMap(m => Array.isArray(m.tags) ? m.tags : []))]
    .sort((a, b) => String(a).localeCompare(String(b), 'en', {sensitivity:'base'}));
}

function renderTagFilter() {
  const tags = uniqueCatalogTags();
  $('#tagOptions').innerHTML = tags.length ? tags.map(tag => `
    <label class="tag-option">
      <input type="checkbox" data-tag-filter="${esc(tag)}" ${state.selectedTags.has(tag) ? 'checked' : ''} />
      ${tagChip(tag)}
    </label>`).join('') : '<span class="filter-empty">В каталоге пока нет тегов</span>';
  const count = state.selectedTags.size;
  $('#tagFilterSummary').textContent = count ? `Теги: ${count}` : 'Теги: все';
  $('#clearTagFilters').disabled = !count;
}

function compareVersions(a, b) {
  const tokenize = value => String(value || '').toLocaleLowerCase('en-US').match(/\d+|[a-z]+/g) || [];
  const aa = tokenize(a), bb = tokenize(b);
  if (!aa.length && !bb.length) return 0;
  if (!aa.length) return -1;
  if (!bb.length) return 1;
  const length = Math.max(aa.length, bb.length);
  for (let i = 0; i < length; i++) {
    if (aa[i] === undefined) return -1;
    if (bb[i] === undefined) return 1;
    if (aa[i] === bb[i]) continue;
    const an = /^\d+$/.test(aa[i]), bn = /^\d+$/.test(bb[i]);
    if (an && bn) return Number(aa[i]) - Number(bb[i]);
    if (an !== bn) return an ? 1 : -1;
    return aa[i].localeCompare(bb[i], 'en');
  }
  return 0;
}

function sortedMods(mods) {
  const direction = state.sortDirection === 'asc' ? 1 : -1;
  return [...mods].sort((a, b) => {
    let result = 0;
    if (state.sortBy === 'version') {
      result = compareVersions(a.latest_mod_version, b.latest_mod_version);
    } else if (state.sortBy === 'tags') {
      const tagsA = [...(Array.isArray(a.tags) ? a.tags : [])].sort((x,y) => String(x).localeCompare(String(y), 'en', {sensitivity:'base'}));
      const tagsB = [...(Array.isArray(b.tags) ? b.tags : [])].sort((x,y) => String(x).localeCompare(String(y), 'en', {sensitivity:'base'}));
      if (!tagsA.length && tagsB.length) result = 1;
      else if (tagsA.length && !tagsB.length) result = -1;
      else result = (tagsA[0] || '').localeCompare(tagsB[0] || '', 'en', {sensitivity:'base'});
      if (!result && tagsA.length !== tagsB.length) result = tagsA.length - tagsB.length;
    } else {
      result = String(a.name || '').localeCompare(String(b.name || ''), 'en', {sensitivity:'base'});
    }
    if (!result) result = String(a.name || '').localeCompare(String(b.name || ''), 'en', {sensitivity:'base'});
    return result * direction;
  });
}

function updateSortUi() {
  const arrow = state.sortDirection === 'asc' ? '↑' : '↓';
  $('#sortBtn').textContent = `Сортировка: ${SORT_LABELS[state.sortBy]} ${arrow}`;
  for (const option of $('#sortMenu').querySelectorAll('[data-sort]')) {
    const active = option.dataset.sort === state.sortBy;
    option.classList.toggle('active', active);
    option.querySelector('span').textContent = active ? arrow : '';
  }
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, ch => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[ch]));
}

function selectionInfo() {
  const byId = new Map(state.mods.map(m => [m.id, m]));
  const selected = new Set();
  const autoReasons = new Map();
  const visiting = new Set();

  function addReason(targetId, sourceName) {
    if (!sourceName || targetId == null) return;
    const arr = autoReasons.get(targetId) || [];
    if (!arr.includes(sourceName)) arr.push(sourceName);
    autoReasons.set(targetId, arr);
  }

  function visit(id, explicit = false) {
    const mod = byId.get(id);
    if (!mod) return;
    if (explicit) state.explicitSelected.add(id);
    selected.add(id);
    if (visiting.has(id)) return;
    visiting.add(id);

    const deps = Array.isArray(mod.required_dependency_ids) ? mod.required_dependency_ids : [];
    for (const depId of deps) {
      const target = byId.get(Number(depId));
      if (target) {
        addReason(target.id, mod.name);
        visit(target.id, false);
      }
    }
    if (mod.parent_mod_id) {
      const parent = byId.get(Number(mod.parent_mod_id));
      if (parent) {
        addReason(parent.id, mod.name);
        visit(parent.id, false);
      }
    }
    visiting.delete(id);
  }

  [...state.explicitSelected].forEach(id => visit(id, true));
  return { selected, autoReasons, autoOnly: new Set([...selected].filter(id => !state.explicitSelected.has(id))) };
}

function filteredMods() {
  const q = state.search.trim().toLocaleLowerCase('ru-RU');
  return sortedMods(state.mods.filter(m => {
    const textMatch = !q || [m.name, m.mod_id, m.mod_db_id, m.mod_db_asset_id, m.short_description, m.description, ...(m.tags || [])]
      .some(v => String(v ?? '').toLocaleLowerCase('ru-RU').includes(q));
    const modTags = new Set((Array.isArray(m.tags) ? m.tags : []).map(tag => String(tag).toLocaleLowerCase('en-US')));
    const tagMatch = [...state.selectedTags].every(tag => modTags.has(String(tag).toLocaleLowerCase('en-US')));
    return textMatch && tagMatch;
  }));
}

function uniqueCatalogTagsSafe(mods) {
  return [...new Set(mods.flatMap(m => Array.isArray(m.tags) ? m.tags : []))];
}

async function loadCatalogData() {
  const res = await fetch('/api/catalog');
  if (!res.ok) return false;
  state.mods = await res.json();
  // Remove filters for tags deleted from the current Discord forum definitions.
  const present = new Set(uniqueCatalogTagsSafe(state.mods));
  state.selectedTags = new Set([...state.selectedTags].filter(tag => present.has(tag)));
  renderTagFilter();
  return true;
}

async function loadCatalog() {
  if (!await loadCatalogData()) return;

  const match = location.pathname.match(/^\/build\/([^/]+)/);
  if (match) {
    try {
      const b = await (await fetch(`/api/builds/${match[1]}`)).json();
      state.explicitSelected = new Set((b.mods || []).filter(m => !m.auto_added).map(m => m.id));
      state.gameVersion = b.target_game_version || '';
      state.buildName = b.name || '';
      $('#gameVersion').value = state.gameVersion;
      render();
    } catch (_) {}
  }
  render();
}

async function pollPrefetchStatus() {
  try {
    const res = await fetch('/api/prefetch/status', { cache: 'no-store' });
    if (res.ok) {
      const status = await res.json();
      if (status.running) {
        state.prefetchWasRunning = true;
        state.prefetchCatalogRefreshed = false;
        const hasFilters = state.search.trim() || state.selectedTags.size;
        const base = hasFilters ? `${filteredMods().length} из ${state.mods.length} модов` : `${state.mods.length} модов в локальной базе`;
        const progress = status.total ? `${status.current}/${status.total}` : 'подготовка';
        state.prefetchStatusText = ` · фоновая загрузка ${progress}`;
        $('#catalogMeta').textContent = `${base}${state.prefetchStatusText}`;
      } else if (state.prefetchWasRunning || (status.current > 0 && String(status.message || '').startsWith('Предзагрузка завершена') && !state.prefetchCatalogRefreshed)) {
        state.prefetchWasRunning = false;
        state.prefetchCatalogRefreshed = true;
        state.prefetchStatusText = '';
        await loadCatalogData();
        render();
      }
    }
  } catch (_) {}
  setTimeout(pollPrefetchStatus, 3000);
}

function render() {
  const info = selectionInfo();
  const visibleMods = filteredMods();
  $('#catalog').innerHTML = visibleMods.map(m => card(m, info)).join('') || '<div class="empty-state">Моды не найдены. Попробуй изменить поиск или фильтры.</div>';
  updateSortUi();
  $('#selectedCount').textContent = info.selected.size;
  $('#selectedCountSmall').textContent = info.selected.size;
  $('#createBtn').disabled = state.explicitSelected.size === 0;
  const visible = visibleMods.length;
  const hasFilters = state.search.trim() || state.selectedTags.size;
  const baseMeta = hasFilters ? `${visible} из ${state.mods.length} модов` : `${state.mods.length} модов в локальной базе`;
  $('#catalogMeta').textContent = `${baseMeta}${state.prefetchStatusText}`;
  renderSelectionList(info);
}

function autoNoteHtml(mod, info) {
  const reasons = info.autoReasons.get(mod.id) || [];
  if (!info.autoOnly.has(mod.id) || !reasons.length) return '';
  return `<div class="auto-note">Зависимость ${esc(reasons.join(', '))}</div>`;
}

function card(m, info) {
  const selected = info.selected.has(m.id);
  const version = m.latest_mod_version ? `v${m.latest_mod_version}` : (m.latest_game_version || 'Версия не указана');
  const gameVersionNote = m.latest_mod_version && m.latest_game_version ? `<small class="game-version-note">Vintage Story ${esc(m.latest_game_version)}</small>` : '';
  const tagsHtml = (Array.isArray(m.tags) && m.tags.length) ? `<div class="mod-tags">${m.tags.map(tagChip).join('')}</div>` : '';
  const displayId = m.display_mod_id || m.mod_id || m.mod_db_id || m.mod_db_asset_id;
  const modId = displayId ? `modid: ${displayId}` : 'modid: не указан';
  const modIdHtml = m.mod_db_url
    ? `<a class="modid" href="${esc(m.mod_db_url)}" target="_blank" rel="noopener">${esc(modId)}</a>`
    : `<span class="modid">${esc(modId)}</span>`;
  const originBadge = m.origin === 'addon' ? '<span class="origin-badge">дополнение</span>' : '';
  return `<article class="card ${selected ? 'is-selected' : ''}" data-card-id="${m.id}" tabindex="0" role="button" aria-pressed="${selected}">
    <div class="card-visual">
      ${m.image_url ? `<img class="cover" loading="lazy" src="${esc(m.image_url)}" alt="" />` : `<div class="cover empty">Нет превью</div>`}
    </div>
    <div class="card-body">
      <div class="title-row"><div class="title">${esc(m.name)} ${originBadge}</div><div class="version-stack"><span class="badge">${esc(version)}</span>${gameVersionNote}</div></div>
      ${tagsHtml}
      <div class="desc">${esc(m.short_description || m.description || 'Описание отсутствует')}</div>
      <div>${modIdHtml}</div>
      ${autoNoteHtml(m, info)}
      <div class="actions">
        <button type="button" data-rel="${m.id}">Связи</button>
      </div>
    </div>
    <div id="rel-${m.id}" class="rel-panel"></div>
  </article>`;
}

function renderSelectionList(info) {
  const selectedMods = state.mods.filter(m => info.selected.has(m.id));
  $('#selectionList').innerHTML = selectedMods.length
    ? `${selectedMods.map(m => {
        const autoOnly = info.autoOnly.has(m.id);
        const reasons = info.autoReasons.get(m.id) || [];
        return `<div class="selection-row">
          <a href="${esc(m.mod_db_url || '#')}" target="_blank" rel="noopener" class="selection-name">
            <span>${esc(m.name)}</span>
            ${autoOnly && reasons.length ? `<small>Зависимость ${esc(reasons.join(', '))}</small>` : ''}
          </a>
          ${autoOnly ? '' : `<button type="button" class="remove-selection" data-remove="${m.id}" aria-label="Убрать ${esc(m.name)}">×</button>`}
        </div>`;
      }).join('')}<button id="clearSelection" class="clear-selection" type="button">Очистить всё</button>`
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
    const name = x.target_url ? `<a href="${esc(x.target_url)}" target="_blank" rel="noopener">${esc(x.target_name || 'Без названия')}</a>` : `<span>${esc(x.target_name || 'Не сопоставлено')}</span>`;
    const lowConfidence = (x.confidence ?? 0) < 0.90;
    const confidenceNote = lowConfidence ? '<em class="rel-note">не учитывается при выборе зависимостей</em>' : '';
    const source = x.source_kind ? `<span class="rel-note">${esc(x.source_kind)}${x.required_version ? ` · ≥ ${esc(x.required_version)}` : ''}</span>` : '';
    return `<div class="rel-line"><div class="rel-main">${name}<small>${esc(x.raw_phrase || '')}</small>${source}${confidenceNote}</div><div class="rel-meta"><strong class="rel-type ${esc(x.relation_type)}">${esc(label)}</strong><span>${esc(confidence)}</span></div></div>`;
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
  } finally { button.disabled = false; }
}

function toggleCard(id) {
  if (state.explicitSelected.has(id)) state.explicitSelected.delete(id);
  else state.explicitSelected.add(id);
  render();
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
pollPrefetchStatus();
    setTimeout(() => $('#progress').classList.add('hidden'), 1400);
  } else if (j.status === 'failed') {
    $('#refreshBtn').disabled = false;
    alert(j.message || 'Ошибка обновления каталога');
    showProgress('Ошибка обновления', j.message || '');
  } else setTimeout(() => pollCatalog(id), 500);
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
  const info = selectionInfo();
  state.pendingBuild = { mod_ids: [...state.explicitSelected], target_game_version: state.gameVersion || null, name: state.buildName };
  $('#buildDialog').close();
  showProgress('Создание сборки', `Разрешение зависимостей: ${info.selected.size} модов…`);
  const res = await fetch('/api/builds/inspect', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(state.pendingBuild) });
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
  $('#shareText').innerHTML = `<strong>${esc(data.name)}</strong><br>${target}<br>Модов в сборке: ${esc(data.mod_count ?? '—')}<br>Share ID: <code>${esc(data.share_id)}</code><br><a href="${esc(data.url)}">${esc(data.url)}</a>`;
  $('#downloadStep').classList.add('hidden');
  $('#publishBtn').disabled = false; $('#noPublish').disabled = false;
  $('#progress').classList.add('hidden'); $('#publishDialog').showModal();
}

async function doPublish() {
  const res = await fetch(`/api/builds/${state.pendingBuild.share_id}/publish`, { method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({publish:true}) });
  const d = await res.json();
  if (!res.ok) { alert(d.detail || 'Ошибка публикации'); return; }
  $('#publishBtn').disabled = true; $('#noPublish').disabled = true;
  $('#shareText').innerHTML += '<br><span class="ok-text">Опубликовано в Discord.</span>';
  $('#downloadStep').classList.remove('hidden');
}

async function beginDownload() {
  const r = await fetch(`/api/builds/${state.pendingBuild.share_id}/prepare`, {method:'POST'});
  const d = await r.json();
  if (!r.ok) { alert(d.detail || 'Не удалось начать подготовку'); return; }
  $('#publishDialog').close();
  showProgress('Подготовка ZIP', 'Загрузка недостающих файлов…');
  pollDownload(d.job_id);
}

async function pollDownload(id) {
  const j = await (await fetch(`/api/jobs/${id}`)).json();
  $('#progressTitle').textContent = 'Подготовка ZIP';
  $('#progressText').textContent = j.message || '';
  $('#progressCount').textContent = j.total ? `${j.current}/${j.total}` : '—';
  $('#progressBar').style.width = (j.total ? Math.min(100, Math.round(j.current / j.total * 100)) : 0) + '%';
  if (j.status === 'done') {
    window.location.href = `/api/builds/${state.pendingBuild.share_id}/download`;
    setTimeout(() => $('#progress').classList.add('hidden'), 2000);
  } else if (j.status === 'failed') {
    alert(j.message || 'Ошибка подготовки');
  } else setTimeout(() => pollDownload(id), 500);
}

document.addEventListener('click', async (e) => {
  const target = e.target;
  const relBtn = target.closest('[data-rel]');
  if (relBtn) { e.stopPropagation(); await toggleRelation(Number(relBtn.dataset.rel), relBtn); return; }
  const modLink = target.closest('.modid');
  if (modLink) { e.stopPropagation(); return; }
  const removeBtn = target.closest('[data-remove]');
  if (removeBtn) { state.explicitSelected.delete(Number(removeBtn.dataset.remove)); render(); return; }
  if (target.closest('#clearSelection')) { state.explicitSelected.clear(); render(); return; }
  const cardEl = target.closest('[data-card-id]');
  if (cardEl && !target.closest('a,button,.rel-panel')) toggleCard(Number(cardEl.dataset.cardId));
});

document.addEventListener('keydown', (e) => {
  const cardEl = e.target.closest?.('[data-card-id]');
  if (!cardEl || e.target.closest('a,button')) return;
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCard(Number(cardEl.dataset.cardId)); }
});

$('#selectionToggle').onclick = () => {
  const panel = $('#selectionPanel');
  const collapsed = panel.classList.toggle('collapsed');
  $('#selectionToggle').setAttribute('aria-expanded', String(!collapsed));
};
$('#searchInput').addEventListener('input', e => { state.search = e.target.value; render(); });
$('#tagOptions').addEventListener('change', e => {
  const input = e.target.closest('[data-tag-filter]');
  if (!input) return;
  if (input.checked) state.selectedTags.add(input.dataset.tagFilter);
  else state.selectedTags.delete(input.dataset.tagFilter);
  renderTagFilter();
  render();
});
$('#clearTagFilters').onclick = () => {
  state.selectedTags.clear();
  renderTagFilter();
  render();
};
$('#sortBtn').onclick = () => {
  const menu = $('#sortMenu');
  menu.classList.toggle('hidden');
  $('#sortBtn').setAttribute('aria-expanded', String(!menu.classList.contains('hidden')));
};
$('#sortMenu').addEventListener('click', e => {
  const option = e.target.closest('[data-sort]');
  if (!option) return;
  const selected = option.dataset.sort;
  if (selected === state.sortBy) {
    state.sortDirection = state.sortDirection === 'asc' ? 'desc' : 'asc';
  } else {
    state.sortBy = selected;
    state.sortDirection = selected === 'version' ? 'desc' : 'asc';
  }
  $('#sortMenu').classList.add('hidden');
  $('#sortBtn').setAttribute('aria-expanded', 'false');
  render();
});
document.addEventListener('click', e => {
  if (!e.target.closest('.sort-wrap')) {
    $('#sortMenu').classList.add('hidden');
    $('#sortBtn').setAttribute('aria-expanded', 'false');
  }
  if (!e.target.closest('#tagFilter')) $('#tagFilter').open = false;
});
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
pollPrefetchStatus();
