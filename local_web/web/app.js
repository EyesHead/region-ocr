(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const formats = { markdown: 'Markdown', html: 'HTML', json: 'JSON', plaintext: 'Текст' };
  const supported = new Set(['pdf', 'docx', 'pptx', 'xlsx', 'html', 'htm', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp', 'gif']);
  const state = { jobs: [], uploading: [], format: 'html', busy: false, connected: false, timer: null, saving: new Set(), opening: new Set(), savingAll: false, installed: [], installedBusy: new Set(), installedToken: 0, installedSearch: '', deleting: new Set(), logRecord: null, logContent: null, logToken: 0, logTrigger: null, filter: 'all', query: '', outputDir: '', storageBusy: false };
  let toastTimer;
  let activeDismissHint;
  let deleteConfirmation;
  let activePageMenu;
  let duplicateDecision;
  let uploadQueue = Promise.resolve();
  let updateSnapshot = null;
  let updateTimer;
  let updateRequestBusy = false;
  const updateLockedControls = new Map();
  function toast(message) {
    $('#toast').textContent = message;
    $('#toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6500);
  }
  async function api(path, options = {}) {
    if (updateSnapshot?.installing && options.method && options.method !== 'GET' && !path.startsWith('/api/updates')) throw new Error('Подготавливается обновление. Дождитесь перезапуска приложения.');
    const headers = { 'X-Local-App': '1', ...(options.headers || {}) };
    const response = await fetch(path, { ...options, headers, cache: 'no-store' });
    let payload;
    try { payload = await response.json(); } catch { throw new Error(`Приложение вернуло некорректный ответ (${response.status}).`); }
    if (!response.ok) {
      const error = new Error(payload.error || payload.message || `Не удалось выполнить запрос (${response.status}).`);
      error.status = response.status; error.payload = payload;
      throw error;
    }
    return payload;
  }
  function post(path, payload) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  }
  function extension(name) { return (name.split('.').pop() || '').toLowerCase(); }
  function sizeText(bytes) {
    if (!Number.isFinite(Number(bytes))) return '';
    if (bytes < 1024) return `${bytes} Б`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} КБ`;
    return `${(bytes / (1024 * 1024)).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} МБ`;
  }
  function countText(number, forms) {
    const last = number % 10;
    const index = number % 100 >= 11 && number % 100 <= 14 ? 2 : last === 1 ? 0 : last >= 2 && last <= 4 ? 1 : 2;
    return `${number} ${forms[index]}`;
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function fileIcon(name) {
    const type = extension(name);
    const families = { pdf: 'pdf', docx: 'word', pptx: 'slides', xlsx: 'sheet', html: 'web', htm: 'web', png: 'image', jpg: 'image', jpeg: 'image', tif: 'image', tiff: 'image', bmp: 'image', webp: 'image', gif: 'image' };
    const family = families[type] || 'other';
    const label = type.slice(0, 4).toUpperCase() || 'FILE';
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 44 54');
    icon.setAttribute('class', `file-icon file-icon-${family} file-type-${type || 'unknown'}`);
    icon.setAttribute('aria-hidden', 'true');
    const marks = {
      pdf: '<path d="m12 27 4-7 4 7 4-4 5 4"/>',
      word: '<path d="m11 21 2 8 3-5 3 5 2-8M25 22h6M25 27h6"/>',
      slides: '<path d="M11 21h20v10H11zM21 31v3M17 34h8M15 28l4-4 4 2 5-3"/>',
      sheet: '<path d="M11 21h20v11H11zM11 26h20M18 21v11M25 21v11"/>',
      web: '<path d="m16 21-5 5 5 5m11-10 5 5-5 5m-4-12-3 14"/>',
      image: '<path d="M11 21h20v12H11zM12 31l6-6 5 5 4-3 4 4"/><circle cx="26" cy="24" r="1"/>',
      other: '<path d="M12 22h18M12 27h13"/>'
    };
    icon.innerHTML = `<path class="file-paper" d="M4 1h22l14 14v37H4z"/><path class="file-fold" d="M26 1v15h14"/><g class="file-mark">${marks[family]}</g><rect class="file-label" x="1" y="35" width="42" height="16" rx="2"/>`;
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', '22'); text.setAttribute('y', '46.5'); text.setAttribute('text-anchor', 'middle');
    text.setAttribute('font-size', label.length > 3 ? '9.5' : '11'); text.setAttribute('font-family', 'Arial, sans-serif'); text.setAttribute('font-weight', '700'); text.setAttribute('fill', 'white');
    text.textContent = label;
    icon.append(text);
    return icon;
  }
  function statusIcon(status) {
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    const shapes = {
      done: '<path d="m5 12 4.5 4.5L19 7"/>',
      queued: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
      running: '<circle cx="12" cy="12" r="9" stroke-dasharray="3 4"/>',
      uploading: '<path d="M12 17V4m-5 5 5-5 5 5M4 16v4h16v-4"/>',
      error: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 3v1"/>',
      cancelled: '<circle cx="12" cy="12" r="9"/><path d="M9 8v8m6-8v8"/>'
    };
    icon.innerHTML = shapes[status] || shapes.queued;
    return icon;
  }
  function addedTime(value) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return '';
    const now = new Date();
    const today = date.toDateString() === now.toDateString();
    const day = today ? 'Сегодня' : date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
    return `${day}, ${date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`;
  }
  function newestFirst(jobs) {
    return jobs.map((job, index) => ({ job, index, added: Date.parse(job.created_at) || 0 }))
      .sort((a, b) => b.added - a.added || a.index - b.index).map(item => item.job);
  }
  function renderUpdates(snapshot) {
    updateSnapshot = snapshot;
    const panel = $('.app-updates');
    panel.classList.toggle('has-update', !!snapshot.available);
    panel.classList.toggle('has-error', !!snapshot.error);
    panel.classList.toggle('is-installing', !!snapshot.installing);
    $('#update-button-label').textContent = snapshot.installing ? 'Подготовка обновления…' : snapshot.checking ? 'Проверка обновлений…' : snapshot.available ? `Обновить до ${snapshot.latest_version}` : 'Проверить обновления';
    $('#update-button').disabled = !!(snapshot.installing || snapshot.checking || updateRequestBusy);
    const version = snapshot.current_version ? `Версия ${snapshot.current_version}` : '';
    $('#update-status').textContent = [version, snapshot.error || snapshot.message || (snapshot.available ? 'Доступна новая версия' : 'Установлена последняя версия')].filter(Boolean).join(' · ');
    if (snapshot.installing) {
      document.querySelectorAll('.controls button, .controls input, .job-actions button, .job-main .text-button, #add-files-button, #clear-button, #output-change, #download-all').forEach(control => {
        if (!updateLockedControls.has(control)) updateLockedControls.set(control, control.disabled);
        control.disabled = true;
      });
    } else if (updateLockedControls.size) {
      updateLockedControls.forEach((disabled, control) => { if (control.isConnected) control.disabled = disabled; });
      updateLockedControls.clear();
    }
  }
  async function refreshUpdates() {
    clearTimeout(updateTimer);
    try { renderUpdates(await api('/api/updates')); }
    catch (error) {
      if (!updateSnapshot?.installing) $('#update-status').textContent = 'Не удалось проверить обновления. Нажмите кнопку, чтобы повторить.';
    } finally { updateTimer = setTimeout(refreshUpdates, updateSnapshot?.checking || updateSnapshot?.installing ? 2000 : 60000); }
  }
  async function checkOrOfferUpdate() {
    if (updateRequestBusy || updateSnapshot?.installing || updateSnapshot?.checking) return;
    if (updateSnapshot?.available) {
      $('#update-description').textContent = `Будет установлена версия ${updateSnapshot.latest_version}. Приложение закроется и откроется снова после обновления. Документы и настройки сохранятся.`;
      $('#update-dialog').showModal();
      return;
    }
    updateRequestBusy = true;
    $('#update-button').disabled = true;
    $('#update-button-label').textContent = 'Проверка обновлений…';
    try { renderUpdates(await post('/api/updates/check', {})); }
    catch (error) { toast(error.message); }
    finally { updateRequestBusy = false; await refreshUpdates(); }
  }
  async function installUpdate() {
    if (updateRequestBusy || updateSnapshot?.installing) return;
    updateRequestBusy = true;
    $('#update-confirm').disabled = true;
    try {
      renderUpdates(await post('/api/updates/install', {}));
      $('#update-dialog').close();
    } catch (error) { toast(error.message); }
    finally { updateRequestBusy = false; $('#update-confirm').disabled = false; await refreshUpdates(); }
  }
  function button(text, className, action) {
    const node = element('button', className, text);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }
  function jobFormat(job) { return Object.hasOwn(formats, job.format) ? job.format : 'markdown'; }
  function isJobRecord(record) { return record.kind === 'job'; }
  function recordPath(record) { return isJobRecord(record) ? `/api/jobs/${encodeURIComponent(record.job_id)}` : `/api/installed/${encodeURIComponent(record.id)}`; }
  function deletionKey(record) { return record.job_id ? `job:${record.job_id}` : record.id; }
  function savedActions(record, disabled = false, original = false) {
    const first = original
      ? button('Открыть оригинал', 'button button-outline', () => openSavedResult(record.id, 'source'))
      : button('Открыть в HTML', 'button button-outline html-preview-button', () => openHtml(`/api/installed/${encodeURIComponent(record.id)}/view/document.html`));
    const actions = [first];
    if (record.format !== 'html') {
      const label = record.format === 'plaintext' ? 'TXT' : formats[record.format] || record.format;
      actions.push(button(`Открыть в ${label}`, 'button button-outline', () => openSavedResult(record.id, 'converted')));
    }
    actions.push(button('Открыть в проводнике', 'button button-outline', () => openSavedResult(record.id, 'folder')));
    actions.forEach(action => { action.disabled = disabled; });
    return actions;
  }
  function deleteButton(record) {
    const remove = button(state.deleting.has(deletionKey(record)) ? 'Удаление…' : 'Удалить', 'button button-danger', () => deleteDocument(record));
    remove.disabled = state.deleting.has(deletionKey(record)) || record.status === 'running';
    remove.setAttribute('aria-label', `Удалить файл и все результаты конвертации: ${record.name}`);
    return remove;
  }
  function additionalActions(job, record) {
    const details = element('details', 'job-more');
    const summary = element('summary', '', '⋯');
    summary.setAttribute('aria-label', `Другие действия с документом ${job.name}`);
    const menu = element('div', 'job-more-menu');
    menu.setAttribute('popover', 'manual'); menu.hidden = true;
    if (record) {
      const disabled = state.opening.has(record.id) || state.deleting.has(deletionKey(record));
      if (record.format !== 'html') {
        const label = record.format === 'plaintext' ? 'TXT' : formats[record.format] || record.format;
        const converted = button(`Открыть в ${label}`, 'button button-outline', () => openSavedResult(record.id, 'converted'));
        converted.disabled = disabled; menu.append(converted);
      }
      const folder = button('Открыть папку результата', 'button button-outline', () => openSavedResult(record.id, 'folder'));
      folder.disabled = disabled; menu.append(folder, deleteButton(record));
    } else {
      menu.append(deleteButton({...job, kind: 'job', job_id: job.id}));
    }
    menu.append(dismissControl(job, true));
    details.append(summary, menu);
    details.addEventListener('toggle', () => {
      if (!details.open) { if (typeof menu.hidePopover === 'function' && menu.matches(':popover-open')) menu.hidePopover(); menu.hidden = true; return; }
      document.querySelectorAll('.job-more[open]').forEach(other => { if (other !== details) other.open = false; });
      menu.hidden = false;
      if (typeof menu.showPopover === 'function') menu.showPopover();
      const bounds = summary.getBoundingClientRect();
      menu.style.left = `${Math.max(8, Math.min(bounds.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
      menu.style.top = `${bounds.bottom + menu.offsetHeight + 8 > innerHeight && bounds.top > menu.offsetHeight + 8 ? bounds.top - menu.offsetHeight - 7 : Math.min(bounds.bottom + 7, innerHeight - menu.offsetHeight - 8)}px`;
    });
    menu.addEventListener('click', event => { if (event.target.closest('button,a')) details.open = false; });
    return details;
  }
  function closeDismissHint() {
    if (!activeDismissHint) return;
    const hint = activeDismissHint;
    activeDismissHint = null;
    if (typeof hint.hidePopover === 'function' && hint.matches(':popover-open')) hint.hidePopover();
    hint.hidden = true;
  }
  function dismissControl(job, inMenu = false) {
    if (inMenu) {
      const action = button('Убрать из списка', 'button button-outline dismiss-menu-action', () => removeJob(job.id));
      action.title = 'Скрыть строку. Документ и результаты останутся в Истории.';
      action.setAttribute('aria-label', `Убрать из списка: ${job.name}`);
      return action;
    }
    const wrapper = element('div', 'dismiss-job');
    const trigger = button('×', 'icon-button', () => removeJob(job.id));
    trigger.setAttribute('aria-label', `Убрать из списка: ${job.name}`);
    const hint = element('div', 'dismiss-hint');
    hint.id = `dismiss-hint-${job.id}`;
    hint.setAttribute('popover', 'manual');
    hint.hidden = true;
    hint.append(document.createTextNode('Убрать из списка. Все файлы всегда можно найти в разделе «'));
    const link = element('a', 'history-link', 'История');
    link.href = '#history';
    link.addEventListener('click', event => { event.preventDefault(); closeDismissHint(); openInstalled(); });
    hint.append(link, document.createTextNode('».'));
    trigger.setAttribute('aria-describedby', hint.id);
    function show() {
      if (activeDismissHint === hint) return;
      closeDismissHint();
      hint.hidden = false;
      if (typeof hint.showPopover === 'function') hint.showPopover();
      const bounds = trigger.getBoundingClientRect();
      hint.style.left = `${Math.max(8, Math.min(bounds.right - hint.offsetWidth, innerWidth - hint.offsetWidth - 8))}px`;
      hint.style.top = `${bounds.bottom + hint.offsetHeight + 8 > innerHeight ? Math.max(8, bounds.top - hint.offsetHeight - 4) : bounds.bottom + 4}px`;
      activeDismissHint = hint;
    }
    function maybeHide() {
      setTimeout(() => { if (!wrapper.matches(':hover') && !wrapper.contains(document.activeElement) && activeDismissHint === hint) closeDismissHint(); }, 180);
    }
    wrapper.addEventListener('mouseenter', show);
    wrapper.addEventListener('mouseleave', maybeHide);
    wrapper.addEventListener('focusin', show);
    wrapper.addEventListener('focusout', maybeHide);
    wrapper.append(trigger, hint);
    return wrapper;
  }
  function openHtml(url) {
    if (window.pywebview?.api?.open_html) {
      window.pywebview.api.open_html(url).catch(error => toast(error.message || String(error)));
      return;
    }
    window.open(url, '_blank', 'noopener,noreferrer');
  }
  function closePageMenu(restoreFocus = false) {
    if (!activePageMenu) return;
    const { menu, trigger } = activePageMenu;
    activePageMenu = null;
    if (typeof menu.hidePopover === 'function' && menu.matches(':popover-open')) menu.hidePopover();
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus && trigger.isConnected) trigger.focus({ preventScroll: true });
  }
  function originalButton(job, caption = 'Оригинал') {
    const key = `source:${job.id}`;
    const open = button(caption, 'button button-outline original-button', async () => {
      if (state.opening.has(key)) return;
      state.opening.add(key); render(); if ($('#installed-dialog').open) renderInstalled();
      try { await post(`/api/jobs/${encodeURIComponent(job.id)}/open`, { target: 'source' }); }
      catch (error) { toast(error.message); }
      finally { state.opening.delete(key); render(); if ($('#installed-dialog').open) renderInstalled(); }
    });
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<path d="M4 2h10l6 6v14H4zM14 2v7h6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>';
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    arrow.setAttribute('viewBox', '0 0 16 16'); arrow.setAttribute('aria-hidden', 'true');
    arrow.innerHTML = '<path d="M4 12 12 4M4 4h8v8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>';
    open.prepend(icon); open.append(arrow);
    open.title = extension(job.name) === 'pdf' ? 'Открыть исходный PDF' : 'Открыть исходный файл';
    open.setAttribute('aria-label', `Открыть оригинал: ${job.name}`);
    open.disabled = job.source_available === false || state.opening.has(key) || state.deleting.has(`job:${job.id}`);
    return open;
  }
  function pageControl(job) {
    const count = Number(job.source_page_count);
    const known = Number.isInteger(count) && count > 0;
    const wrapper = element('div', 'page-control');
    const allPages = count === 1 ? 'Вся 1 страница' : `Все ${countText(count || 0, ['страница', 'страницы', 'страниц'])}`;
    const summary = job.pages ? `Страницы ${job.pages.replace('-', '–')}` : known ? allPages : 'Страницы недоступны';
    const trigger = button(summary, 'button button-outline page-trigger', () => {
      if (activePageMenu?.trigger === trigger) { closePageMenu(true); return; }
      closePageMenu(); closeDismissHint();
      menu.hidden = false;
      if (typeof menu.showPopover === 'function') menu.showPopover();
      const bounds = trigger.getBoundingClientRect();
      menu.style.left = `${Math.max(8, Math.min(bounds.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
      menu.style.top = `${bounds.bottom + menu.offsetHeight + 8 > innerHeight && bounds.top > menu.offsetHeight + 8 ? bounds.top - menu.offsetHeight - 7 : Math.min(bounds.bottom + 7, innerHeight - menu.offsetHeight - 8)}px`;
      trigger.setAttribute('aria-expanded', 'true');
      activePageMenu = { menu, trigger, wrapper };
      (range.checked ? from : all).focus({ preventScroll: true });
    });
    const chevron = element('span', 'page-chevron', '⌄'); chevron.setAttribute('aria-hidden', 'true'); trigger.append(chevron);
    trigger.setAttribute('aria-label', `Выбрать страницы: ${job.name}`);
    trigger.setAttribute('aria-haspopup', 'dialog'); trigger.setAttribute('aria-expanded', 'false');
    trigger.disabled = !known || job.pages_editable === false || !['queued', 'error', 'cancelled'].includes(job.status) || state.busy;
    trigger.title = !known ? job.page_count_error || 'Не удалось определить число страниц PDF' : job.status === 'done' ? 'Диапазон обработанного документа' : 'Выбрать страницы только для этого PDF';
    const menu = element('div', 'page-menu'); menu.hidden = true;
    menu.id = `page-menu-${job.id}`; menu.setAttribute('popover', 'manual'); menu.setAttribute('role', 'dialog');
    menu.setAttribute('aria-label', `Страницы PDF: ${job.name}`); trigger.setAttribute('aria-controls', menu.id);
    menu.append(element('h3', '', 'Страницы этого PDF'));
    const allLabel = element('label', 'page-mode'); const all = element('input');
    all.type = 'radio'; all.name = `page-mode-${job.id}`; all.checked = !job.pages;
    allLabel.append(all, document.createTextNode(allPages));
    const rangeLabel = element('label', 'page-mode'); const range = element('input');
    range.type = 'radio'; range.name = all.name; range.checked = !!job.pages;
    rangeLabel.append(range, document.createTextNode('Диапазон'));
    const fields = element('div', 'page-range-fields');
    const from = element('select'); const to = element('select');
    from.id = `page-from-${job.id}`; to.id = `page-to-${job.id}`;
    from.setAttribute('aria-label', `Первая страница: ${job.name}`); to.setAttribute('aria-label', `Последняя страница: ${job.name}`);
    for (let number = 1; number <= count; number++) { from.append(element('option', '', String(number))); to.append(element('option', '', String(number))); }
    const limits = job.pages ? job.pages.split('-').map(Number) : [1, count];
    from.value = String(limits[0]); to.value = String(limits[1]);
    const fromLabel = element('label', '', 'С'); fromLabel.htmlFor = from.id;
    const toLabel = element('label', '', 'По'); toLabel.htmlFor = to.id;
    fields.append(fromLabel, from, toLabel, to);
    const selected = element('p', 'page-selected');
    const update = () => { from.disabled = to.disabled = !range.checked; selected.textContent = `Выбрано ${countText(range.checked ? Number(to.value) - Number(from.value) + 1 : count, ['страница', 'страницы', 'страниц'])}`; };
    all.addEventListener('change', update); range.addEventListener('change', update);
    from.addEventListener('change', () => { if (Number(from.value) > Number(to.value)) to.value = from.value; update(); });
    to.addEventListener('change', () => { if (Number(to.value) < Number(from.value)) from.value = to.value; update(); });
    const controls = element('div', 'page-menu-actions');
    const cancel = button('Отмена', 'button button-outline', () => closePageMenu(true));
    const apply = button('Применить', 'button button-primary', async () => {
      apply.disabled = cancel.disabled = true;
      try {
        await post(`/api/jobs/${encodeURIComponent(job.id)}/pages`, { pages: range.checked ? `${from.value}-${to.value}` : '' });
        closePageMenu(true); await refresh();
      } catch (error) { toast(error.message); }
      finally { apply.disabled = cancel.disabled = false; }
    });
    controls.append(cancel, apply); menu.append(allLabel, rangeLabel, fields, selected, controls);
    trigger.addEventListener('keydown', event => { if (event.key === 'ArrowDown' && !trigger.disabled) { event.preventDefault(); trigger.click(); } });
    menu.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePageMenu(true); } });
    update(); wrapper.append(trigger, menu); return wrapper;
  }
  function createJobRow(job) {
    const status = job.status || 'queued';
    const convertedFormat = jobFormat(job);
    const row = element('article', 'job-row');
    row.dataset.id = job.id;
    row.dataset.status = status;
    if (job.legacy_export) row.dataset.needsExport = 'true';
    row.setAttribute('role', 'listitem');
    row.append(fileIcon(job.name));
    const main = element('div', 'job-main');
    const name = element('h3', 'job-name', job.name);
    name.title = job.name;
    main.append(name);
    const meta = [];
    const added = addedTime(job.created_at);
    if (added) meta.push(`Добавлен: ${added}`);
    if (job.pages) meta.push(`Выбраны страницы ${job.pages.replace('-', '–')}`);
    if (job.size !== undefined) meta.push(sizeText(job.size));
    if (status === 'done') meta.push(`→ ${formats[convertedFormat]}`);
    main.append(element('p', 'job-meta', meta.join(' · ')));
    const pages = extension(job.name) === 'pdf' ? job.source_page_count || job.stats?.pages : job.stats?.pages;
    const pageCount = element('p', 'job-pages', pages ? countText(pages, extension(job.name) === 'xlsx' ? ['лист', 'листа', 'листов'] : ['страница', 'страницы', 'страниц']) : '');
    const statusRegion = element('div', 'job-state');
    if (status === 'running') {
      const progress = Math.min(100, Math.max(0, Number(job.progress) || 0));
      const line = element('div', 'progress-line');
      const track = element('div', 'progress-track');
      track.setAttribute('role', 'progressbar');
      track.setAttribute('aria-label', `Обработка ${job.name}`);
      track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', String(progress));
      const fill = element('div', 'progress-fill');
      fill.style.width = `${progress}%`;
      track.append(fill);
      line.append(track, element('span', 'progress-percent', `${Math.round(progress)}%`));
      statusRegion.append(line);
    }
    const stage = status === 'error' ? job.user_error || 'Не удалось преобразовать документ. Повторите попытку.' : job.stage;
    if (stage && status !== 'done') main.append(element('p', `job-stage${status === 'error' ? ' error' : ''}`, stage));
    if (status === 'done' && (job.legacy_export || convertedFormat !== state.format)) {
      if (job.legacy_export) main.append(element('p', 'job-stage', 'Обновите экспорт для новой структуры папки и внешних изображений.'));
      const reexport = button(`Экспортировать в ${formats[state.format]}`, 'text-button', () => exportJobs([job.id], state.format));
      reexport.title = 'Подготовить выбранный формат из сохранённого результата без повторного OCR';
      reexport.disabled = state.busy;
      main.append(reexport);
    }
    row.append(main, pageCount, statusRegion);
    const actions = element('div', 'job-actions');
    const statusNames = { queued: 'В очереди', running: 'Обработка', done: 'OCR выполнен', error: 'Ошибка', cancelled: 'Остановлено', uploading: 'Загрузка' };
    const needsExport = status === 'done' && job.legacy_export;
    const statusLabel = element('span', `job-status ${needsExport ? 'queued' : status}`, needsExport ? 'Нужен экспорт' : statusNames[status] || status);
    statusLabel.prepend(statusIcon(needsExport ? 'queued' : status));
    statusRegion.prepend(statusLabel);
    if (status !== 'uploading') {
      const sources = element('div', 'job-source-controls');
      sources.append(originalButton(job));
      if (extension(job.name) === 'pdf') sources.append(pageControl(job));
      actions.append(sources);
    }
    if (status === 'done' && !job.legacy_export) {
      if (job.saved_result_id) {
        const record = { id: job.saved_result_id, job_id: job.id, name: job.name, format: convertedFormat };
        const disabled = state.opening.has(record.id) || state.deleting.has(deletionKey(record));
        const open = button('Открыть результат', 'button button-primary html-preview-button', () => openHtml(`/api/installed/${encodeURIComponent(record.id)}/view/document.html`));
        open.disabled = disabled;
        actions.append(open, additionalActions(job, record));
      } else {
        actions.append(button('Открыть результат', 'button button-primary html-preview-button', () => openHtml(`/api/view/${encodeURIComponent(job.id)}/document.html`)));
        const download = button(state.saving.has(job.id) ? 'Сохранение…' : 'Скачать', 'button button-outline', () => saveResult(job.id));
        download.disabled = state.saving.has(job.id) || state.savingAll;
        download.title = 'Сохранить выбранный формат и ресурсы в папку результата';
        download.setAttribute('aria-label', `Скачать ${job.name} в формате ${formats[convertedFormat]}`);
        actions.append(download, additionalActions(job));
      }
    } else if (status === 'running') {
      actions.append(button('Остановить', 'button button-outline', () => cancelJobs([job.id])));
    } else if (status === 'error' || status === 'cancelled') {
      actions.append(button('Повторить', 'button button-outline', () => startJobs([job.id])));
    }
    if (status !== 'running' && status !== 'uploading' && !(status === 'done' && !job.legacy_export)) {
      actions.append(additionalActions(job));
    }
    row.append(actions);
    return row;
  }
  function restoreRowFocus(focusId, focusLabel) {
    const row = [...$('#job-list').children].find(node => node.dataset.id === focusId);
    const control = row && [...row.querySelectorAll('button,a,summary')].find(node => (node.getAttribute('aria-label') || node.textContent) === focusLabel);
    if (control) { const details = control.closest('details'); if (details && control.tagName !== 'SUMMARY') details.open = true; control.focus({ preventScroll: true }); }
  }
  function render() {
    const jobs = newestFirst([...state.jobs, ...state.uploading]);
    const normalizedQuery = state.query.trim().toLocaleLowerCase('ru-RU');
    const visibleJobs = jobs.filter(job => (!normalizedQuery || job.name.toLocaleLowerCase('ru-RU').includes(normalizedQuery))
      && (state.filter === 'all' || state.filter === 'done' && job.status === 'done' && !job.legacy_export || state.filter === 'active' && ['queued', 'running', 'uploading'].includes(job.status)));
    const completed = state.jobs.filter(job => job.status === 'done' && !job.legacy_export);
    const needsExport = state.jobs.filter(job => job.status === 'done' && job.legacy_export);
    const running = state.jobs.filter(job => job.status === 'running');
    const queued = state.jobs.filter(job => job.status === 'queued');
    const failures = state.jobs.filter(job => job.status === 'error');
    const active = running.length > 0;
    const previousScroll = $('#job-list').scrollTop;
    const focused = document.activeElement;
    const focusedRow = focused?.closest('.job-row');
    const focusId = focusedRow?.dataset.id;
    const focusLabel = focused?.getAttribute('aria-label') || focused?.textContent;
    const list = $('#job-list');
    const existingRows = new Map([...list.children].map(row => [row.dataset.id, row]));
    const nextRows = visibleJobs.map(job => {
      const key = JSON.stringify([state.format, state.busy, state.savingAll, state.saving.has(job.id), state.opening.has(job.saved_result_id), state.opening.has(`source:${job.id}`), state.deleting.has(`job:${job.id}`), job]);
      const previous = existingRows.get(job.id);
      if (previous?.dataset.renderKey === key) return previous;
      const row = createJobRow(job);
      if (previous?.querySelector('.job-more')?.open && row.querySelector('.job-more')) row.querySelector('.job-more').open = true;
      row.dataset.renderKey = key;
      return row;
    });
    nextRows.forEach((row, index) => { if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null); });
    const retained = new Set(nextRows);
    if (activePageMenu && !nextRows.some(row => row.contains(activePageMenu.wrapper))) closePageMenu();
    [...list.children].forEach(row => { if (!retained.has(row)) { if (activeDismissHint && row.contains(activeDismissHint)) closeDismissHint(); row.remove(); } });
    $('#job-list').scrollTop = previousScroll;
    if (focusId && !focused.isConnected) {
      restoreRowFocus(focusId, focusLabel);
    }
    $('#empty-state').hidden = visibleJobs.length > 0;
    $('#empty-state h3').textContent = jobs.length ? 'Документы не найдены' : 'Добавьте документы';
    $('#empty-state > p').textContent = jobs.length ? 'Измените поиск или выберите другое состояние документов' : 'Перетащите файлы в область слева или выберите их на компьютере';
    $('#empty-state .empty-detail').hidden = jobs.length > 0;
    $('#nav-all-count').textContent = jobs.length;
    $('#nav-done-count').textContent = completed.length;
    $('#filter-all-count').textContent = jobs.length;
    $('#filter-active-count').textContent = jobs.filter(job => ['queued', 'running', 'uploading'].includes(job.status)).length;
    $('#filter-done-count').textContent = completed.length;
    $('#download-all').hidden = !completed.some(job => !job.saved_result_id);
    $('#download-all').disabled = state.savingAll || state.saving.size > 0 || !state.connected;
    $('#download-all').textContent = state.savingAll ? 'Сохранение…' : 'Скачать всё';
    $('#clear-button').hidden = jobs.length === 0 || active || state.uploading.length > 0;
    $('#cancel-all-button').hidden = !active;
    $('#convert-button').disabled = queued.length === 0 || state.busy || state.uploading.length > 0 || !state.connected;
    $('#convert-button').textContent = queued.length ? `Обработать ${countText(queued.length, ['файл', 'файла', 'файлов'])}` : active ? 'Обработка идёт' : 'Обработать документы';
    $('#selection-note').textContent = state.uploading.length ? 'Загрузка файлов…' : queued.length ? `${countText(queued.length, ['файл готов', 'файла готовы', 'файлов готовы'])} к обработке` : 'Добавьте файлы для преобразования';
    if (active) {
      $('#screen-title').textContent = 'Документы обрабатываются';
      $('#screen-subtitle').textContent = `Готово: ${completed.length} из ${state.jobs.length}`;
    } else if (jobs.length && completed.length === state.jobs.length && !state.uploading.length) {
      $('#screen-title').textContent = 'Документы готовы';
      $('#screen-subtitle').textContent = `Готово: ${completed.length} из ${state.jobs.length}`;
    } else if (jobs.length) {
      $('#screen-title').textContent = 'Конвертер документов';
      $('#screen-subtitle').textContent = failures.length || needsExport.length ? `Готово: ${completed.length}${failures.length ? ` · С ошибкой: ${failures.length}` : ''}${needsExport.length ? ` · Нужен экспорт: ${needsExport.length}` : ''}` : `Добавлено ${countText(jobs.length, ['документ', 'документа', 'документов'])}`;
    } else {
      $('#screen-title').textContent = 'Конвертер документов';
      $('#screen-subtitle').textContent = 'Документы и сканы → структурированный результат';
    }
    $('#screen-title').textContent = 'Документы';
    $('#queue-summary').textContent = jobs.length ? `${countText(jobs.length, ['документ', 'документа', 'документов'])}${completed.length ? ` · Готово: ${completed.length}` : ''}${needsExport.length ? ` · Нужен экспорт: ${needsExport.length}` : ''}${queued.length ? ` · В очереди: ${queued.length}` : ''}` : 'Файлы появятся здесь после загрузки';
    $('#results-footnote').textContent = completed.length ? 'Готовые документы можно открыть или сохранить.' : active ? 'Документы обрабатываются последовательно. Можно продолжать добавлять файлы.' : 'Файлы и результаты доступны в «Истории»';
    const notes = {
      markdown: 'Для заметок и текстовых редакторов. Сохраняет структуру документа.',
      html: 'Для просмотра в браузере. Сохраняет таблицы, формулы и изображения.',
      json: 'Для дальнейшей обработки данных. Сохраняет структуру таблиц и ссылки на изображения.',
      plaintext: 'Текст без оформления. Для таблиц и формул выберите HTML или JSON.'
    };
    $('#format-note').textContent = notes[state.format];
    if (updateSnapshot?.installing) renderUpdates(updateSnapshot);
  }
  function updateOutputDirectory(value, error = null) {
    if (!value) return;
    state.outputDir = value;
    if (error !== null) { $('#output-storage-error').textContent = error; $('#output-storage-error').hidden = !error; }
    $('#output-directory').textContent = value;
    $('#output-directory').title = value;
    $('#installed-root').textContent = value;
    $('#download-all').title = `Сохранить все готовые результаты в ${value}`;
  }
  async function openOutputSettings() {
    $('#output-error').hidden = true;
    if (!state.outputDir) {
      try { updateOutputDirectory((await api('/api/settings')).output_dir); }
      catch (error) { toast(error.message); return; }
    }
    $('#output-path').value = state.outputDir;
    $('#output-browse').hidden = !window.pywebview?.api?.choose_output_directory;
    $('#output-dialog').showModal();
    $('#output-path').focus();
  }
  function outputError(error) {
    $('#output-error').textContent = error.message || String(error);
    $('#output-error').hidden = false;
  }
  async function saveOutputSettings(event) {
    event.preventDefault();
    if (state.storageBusy) return;
    state.storageBusy = true;
    $('#output-save').disabled = true;
    $('#output-save').textContent = 'Сохранение…';
    $('#output-cancel').disabled = true;
    $('#output-browse').disabled = true;
    $('#output-error').hidden = true;
    try {
      const settings = await post('/api/settings', {output_dir: $('#output-path').value.trim()});
      updateOutputDirectory(settings.output_dir);
      $('#output-dialog').close();
      toast('Папка сохранения изменена. Существующие файлы остались на своих местах.');
      await refresh();
    } catch (error) { outputError(error); }
    finally {
      state.storageBusy = false;
      $('#output-save').disabled = false;
      $('#output-save').textContent = 'Сохранить';
      $('#output-cancel').disabled = false;
      $('#output-browse').disabled = false;
    }
  }
  async function browseOutputDirectory() {
    $('#output-browse').disabled = true;
    $('#output-error').hidden = true;
    try {
      const selected = await window.pywebview.api.choose_output_directory();
      if (selected) $('#output-path').value = selected;
    } catch (error) { outputError(error); }
    finally { $('#output-browse').disabled = false; }
  }
  async function refresh() {
    clearTimeout(state.timer);
    try {
      const result = await api('/api/status');
      state.jobs = result.jobs || [];
      updateOutputDirectory(result.output_dir, result.storage_error || '');
      state.connected = true;
      $('#connection-error').hidden = true;
      const label = result.engine_ready ? 'Готов к обработке' : ['starting', 'warming'].includes(result.engine_runtime?.phase) ? 'Подготовка к обработке…' : 'Обработка пока недоступна';
      $('#engine-label').textContent = label;
      $('#engine-status').className = `engine-status ${result.engine_runtime?.phase === 'warming' ? 'warming' : result.engine_ready ? 'ready' : 'unavailable'}`;
      $('#engine-status').title = label;
      render();
    } catch (error) {
      state.connected = false;
      $('#connection-error').hidden = false;
      $('#engine-label').textContent = 'Нет соединения';
      $('#engine-status').className = 'engine-status unavailable';
      $('#convert-button').disabled = true;
    } finally {
      state.timer = setTimeout(refresh, state.jobs.some(job => job.status === 'running') || state.uploading.length ? 900 : 2500);
    }
  }
  async function uploadFiles(files) {
    if (updateSnapshot?.installing) { toast('Подготавливается обновление. Дождитесь перезапуска приложения.'); return; }
    const valid = [];
    const invalid = [];
    for (const file of files) (supported.has(extension(file.name)) ? valid : invalid).push(file);
    if (invalid.length) toast(`Формат не поддерживается: ${invalid.map(file => file.name).join(', ')}`);
    const localJobs = valid.map((file, index) => ({ id: `upload-${Date.now()}-${index}-${Math.random()}`, name: file.name, size: file.size, created_at: new Date().toISOString(), status: 'uploading', stage: 'Проверка файла…', file }));
    state.uploading.push(...localJobs);
    render();
    const uploadBatch = async () => {
      for (const job of localJobs) {
        try { await uploadWithDuplicateCheck(job); }
        catch (error) { toast(`${job.name}: ${error.message}`); }
        finally {
          state.uploading = state.uploading.filter(item => item.id !== job.id);
          await refresh();
        }
      }
    };
    const task = uploadQueue.then(uploadBatch, uploadBatch);
    uploadQueue = task.catch(() => {});
    await task;
  }
  function chooseDuplicate(job, duplicate) {
    closePageMenu(); closeDismissHint();
    $('#duplicate-name').textContent = job.name;
    const existing = duplicate.existing || {};
    const canReuse = duplicate.can_use_existing !== false;
    const active = ['running', 'queued'].includes(existing.status) && duplicate.can_replace === false;
    $('#duplicate-message').textContent = !canReuse
      ? 'Файл с таким именем сейчас загружается. Дождитесь завершения загрузки и добавьте файл ещё раз.'
      : active
      ? 'Файл с таким именем уже обрабатывается или ожидает обработки. Можно использовать прежнюю загрузку.'
      : 'Файл с таким именем уже есть в приложении. Используйте прежний файл и его результаты или загрузите новую копию.';
    $('#duplicate-details').textContent = duplicate.can_replace === false
      ? duplicate.reason || 'Замена станет доступна после завершения обработки.'
      : `${duplicate.count > 1 ? `Найдено прежних загрузок: ${duplicate.count}. ` : ''}При замене прежние результаты будут удалены. Новую копию потребуется конвертировать.`;
    $('#duplicate-replace').disabled = duplicate.can_replace === false;
    $('#duplicate-reuse').disabled = !canReuse;
    const trigger = document.activeElement;
    return new Promise(resolve => {
      duplicateDecision = { resolve, trigger };
      job.stage = 'Файл уже загружен — выберите действие'; render();
      $('#duplicate-dialog').showModal();
      $(canReuse ? '#duplicate-reuse' : '#duplicate-cancel').focus({ preventScroll: true });
    });
  }
  function finishDuplicate(choice) {
    if (!duplicateDecision || (choice === 'replace' && $('#duplicate-replace').disabled)
        || (choice === 'reuse' && $('#duplicate-reuse').disabled)) return;
    const decision = duplicateDecision;
    duplicateDecision = null;
    $('#duplicate-dialog').close();
    decision.resolve(choice);
    if (decision.trigger?.isConnected) decision.trigger.focus({ preventScroll: true });
  }
  function duplicatePayload(payload) {
    if (!payload?.duplicate) return null;
    return typeof payload.duplicate === 'object' ? payload.duplicate : payload;
  }
  async function uploadWithDuplicateCheck(job) {
    let check;
    try { check = await post('/api/upload-check', { name: job.name, size: job.size }); }
    catch (error) {
      if (error.status === 404) throw new Error('Для защиты от повторной загрузки требуется применить обновление приложения после завершения текущей обработки.');
      throw error;
    }
    while (true) {
      let replaceToken = '';
      if (check.duplicate) {
        const choice = await chooseDuplicate(job, check);
        if (choice === 'cancel') return;
        if (choice === 'reuse') {
          try {
            const result = await post('/api/upload-existing', { token: check.token });
            const existing = result.existing || result.saved;
            if (existing?.kind === 'saved' || result.job?.hidden || (result.saved && !result.job)) {
              state.installedSearch = job.name; $('#installed-search').value = job.name;
              await openInstalled();
            }
            toast('Используется ранее загруженный файл. Повторная копия не создана.');
            return;
          } catch (error) {
            const next = duplicatePayload(error.payload);
            if (next) { check = next; continue; }
            throw error;
          }
        }
        replaceToken = check.token;
      }
      job.stage = replaceToken ? 'Загрузка новой копии…' : 'Копирование исходника…'; render();
      try {
        const suffix = replaceToken ? `&replace_token=${encodeURIComponent(replaceToken)}` : '';
        const result = await api(`/api/upload?name=${encodeURIComponent(job.name)}${suffix}`, {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: job.file });
        (result.warnings || []).forEach(message => toast(message));
        return;
      } catch (error) {
        const next = duplicatePayload(error.payload);
        if (next) { check = next; continue; }
        throw error;
      }
    }
  }
  async function startJobs(ids) {
    closePageMenu();
    state.busy = true;
    render();
    try { await post('/api/start', { ids, format: state.format }); }
    catch (error) { toast(error.message); }
    finally { state.busy = false; await refresh(); }
  }
  async function cancelJobs(ids) {
    try { await post('/api/cancel', { ids }); await refresh(); }
    catch (error) { toast(error.message); }
  }
  async function exportJobs(ids, format) {
    if (state.busy) return;
    state.busy = true;
    render();
    try { await post('/api/export', { ids, format }); }
    catch (error) { toast(error.message); }
    finally { state.busy = false; await refresh(); }
  }
  async function saveResult(id) {
    if (state.saving.has(id) || state.savingAll) return;
    state.saving.add(id);
    render();
    if ($('#installed-dialog').open) renderInstalled();
    try {
      const result = await post('/api/save', { id });
      if (!result.saved?.id) throw new Error('Приложение не подтвердило сохранение результата.');
      const job = state.jobs.find(item => item.id === id);
      if (job) job.saved_result_id = result.saved.id;
      toast(`Сохранено: ${result.saved.folder_name || result.saved.name}. Доступно открытие HTML, результата и папки.`);
      await refresh();
      if ($('#installed-dialog').open) await loadInstalled();
    } catch (error) { toast(error.message); }
    finally { state.saving.delete(id); render(); if ($('#installed-dialog').open) renderInstalled(); }
  }
  async function saveAllResults() {
    if (state.savingAll || state.saving.size) return;
    state.savingAll = true;
    render();
    try {
      const result = await post('/api/save-all', {});
      const items = result.items || [];
      const errors = result.errors || [];
      toast(errors.length ? `Сохранено: ${items.length}. Не удалось сохранить: ${errors.length}. Проверьте результаты в «Истории».` : `Сохранено ${countText(items.length, ['документ', 'документа', 'документов'])} в ocr_output.`);
      await refresh();
      if ($('#installed-dialog').open) await loadInstalled();
    } catch (error) { toast(error.message); }
    finally { state.savingAll = false; render(); }
  }
  async function openSavedResult(id, target) {
    if (!id || state.opening.has(id)) return;
    state.opening.add(id);
    render();
    if ($('#installed-dialog').open) renderInstalled();
    try { await post(`/api/installed/${encodeURIComponent(id)}/open`, { target }); }
    catch (error) { toast(error.message); }
    finally { state.opening.delete(id); render(); if ($('#installed-dialog').open) renderInstalled(); }
  }
  function savedDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
  }
  function renderInstalled() {
    const query = state.installedSearch.trim().normalize('NFKC').toLocaleLowerCase('ru-RU');
    const items = state.installed.filter(item => `${item.name} ${item.folder_name || ''}`.normalize('NFKC').toLocaleLowerCase('ru-RU').includes(query));
    const rows = items.map(item => {
      const row = element('article', 'installed-row');
      row.setAttribute('role', 'listitem');
      row.dataset.id = item.id;
      const description = element('div', 'installed-description');
      description.append(element('h3', '', item.folder_name || item.name));
      const statuses = { queued: 'В очереди', running: 'Обработка', error: 'Ошибка', cancelled: 'Остановлено', done: 'Не скачан' };
      description.append(element('p', 'installed-meta', [formats[item.format] || item.format, isJobRecord(item) ? statuses[item.status] : 'Скачан', savedDate(item.created_at), sizeText(item.size)].filter(Boolean).join(' · ')));
      if (item.path && !isJobRecord(item)) description.append(element('p', 'installed-path', item.path));
      if (item.exists === false) description.append(element('p', 'installed-missing', 'Папка не найдена на диске'));
      const actions = element('div', 'installed-actions');
      const disabled = item.exists === false || state.deleting.has(deletionKey(item)) || state.opening.has(item.id);
      if (isJobRecord(item)) {
        actions.append(originalButton({ ...item, id: item.job_id }, 'Открыть оригинал'));
        if (item.status === 'done' && !item.legacy_export) {
          const download = button(state.saving.has(item.job_id) ? 'Сохранение…' : 'Скачать', 'button button-primary', () => saveResult(item.job_id));
          download.disabled = disabled || state.saving.has(item.job_id) || state.savingAll;
          actions.append(download);
        }
      } else {
        actions.append(...savedActions(item, disabled, true));
      }
      const copy = button('Копировать лог', 'button button-outline', () => copySavedLog(item));
      actions.append(copy, deleteButton(item));
      row.append(description, actions);
      return row;
    });
    $('#installed-list').replaceChildren(...rows);
    $('#installed-summary').textContent = query ? `Найдено: ${items.length} из ${state.installed.length}` : countText(state.installed.length, ['документ', 'документа', 'документов']);
    $('#installed-message').hidden = items.length > 0;
    $('#installed-message').className = 'installed-message';
    $('#installed-message').textContent = query ? 'По этому названию ничего не найдено.' : 'История пока пуста. Добавьте файл для конвертации.';
  }
  async function loadInstalled(reconcile = false) {
    const token = ++state.installedToken;
    $('#installed-refresh').disabled = true;
    $('#installed-message').hidden = false;
    $('#installed-message').className = 'installed-message';
    $('#installed-message').textContent = 'Загрузка сохранённых результатов…';
    try {
      const result = reconcile ? await post('/api/installed/refresh', {}) : await api('/api/installed');
      if (token !== state.installedToken) return;
      state.installed = result.items || [];
      updateOutputDirectory(result.output_dir);
      renderInstalled();
    } catch (error) {
      if (token !== state.installedToken) return;
      $('#installed-message').hidden = false;
      $('#installed-message').className = 'installed-message error';
      $('#installed-message').textContent = `Не удалось получить сохранённые результаты: ${error.message}`;
    } finally { if (token === state.installedToken) $('#installed-refresh').disabled = false; }
  }
  async function openInstalled() {
    closePageMenu();
    closeDismissHint();
    $('#installed-dialog').showModal();
    $('#installed-search').focus({ preventScroll: true });
    await loadInstalled();
  }
  function confirmDeletion(record) {
    closePageMenu();
    if ($('#delete-dialog').open) return Promise.resolve(false);
    closeDismissHint();
    $('#delete-message').textContent = `Вы подтверждаете, что хотите полностью удалить файл и результаты конвертации «${record.name}»?`;
    const trigger = document.activeElement;
    return new Promise(resolve => {
      deleteConfirmation = { resolve, trigger };
      $('#delete-dialog').showModal();
      $('#delete-cancel').focus({ preventScroll: true });
    });
  }
  async function deleteDocument(item) {
    const key = deletionKey(item);
    if (state.deleting.has(key) || !await confirmDeletion(item)) return;
    state.deleting.add(key);
    render();
    if ($('#installed-dialog').open) renderInstalled();
    try {
      await api(recordPath(item), { method: 'DELETE' });
      await refresh();
      if ($('#installed-dialog').open) await loadInstalled();
      toast('Файл, все результаты конвертации, логи и кэш удалены.');
    } catch (error) {
      toast(error.message);
      await refresh();
      if ($('#installed-dialog').open) await loadInstalled();
    }
    finally { state.deleting.delete(key); render(); if ($('#installed-dialog').open) renderInstalled(); }
  }
  function setLogContent(record, content) {
    state.logRecord = record;
    state.logContent = content;
    $('#log-title').textContent = record.folder_name || record.name;
    $('#log-content').textContent = content;
    $('#log-content').hidden = false;
    $('#log-copy-fallback').hidden = true;
    $('#log-copy-fallback').value = '';
    $('#log-message').hidden = !!content;
    $('#log-message').textContent = 'Лог конвертации пуст.';
    $('#log-caption').textContent = 'Полный текст без сокращений';
    $('#copy-log-button').disabled = false;
  }
  async function copyLogContent(record, content) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(content);
      toast('Полный лог скопирован в буфер обмена.');
    } catch {
      if (!$('#log-dialog').open) {
        state.logTrigger = document.activeElement;
        $('#log-dialog').showModal();
      }
      setLogContent(record, content);
      $('#log-content').hidden = true;
      $('#log-copy-fallback').hidden = false;
      $('#log-copy-fallback').value = content;
      $('#log-message').hidden = false;
      $('#log-message').textContent = 'Автоматическое копирование недоступно. Текст выделен: нажмите Ctrl+C (или Cmd+C).';
      $('#log-copy-fallback').focus();
      $('#log-copy-fallback').select();
    }
  }
  async function copySavedLog(record) {
    try {
      const result = await api(`${recordPath(record)}/log`);
      if (typeof result.content !== 'string') throw new Error('Приложение не вернуло текст лога.');
      await copyLogContent(record, result.content);
    } catch (error) { toast(`Не удалось скопировать лог: ${error.message}`); }
  }
  async function removeJob(id) {
    closeDismissHint();
    try { await post(`/api/jobs/${encodeURIComponent(id)}/hide`, {}); await refresh(); }
    catch (error) { toast(error.message); }
  }
  $('#choose-button').addEventListener('click', () => $('#file-input').click());
  $('#add-files-button').addEventListener('click', () => $('#file-input').click());
  $('#job-search').addEventListener('input', event => { state.query = event.target.value; render(); });
  document.querySelectorAll('[data-job-filter]').forEach(node => node.addEventListener('click', () => {
    state.filter = node.dataset.jobFilter;
    document.querySelectorAll('[data-job-filter]').forEach(item => {
      const selected = item.dataset.jobFilter === state.filter;
      item.classList.toggle('selected', selected); item.setAttribute('aria-pressed', String(selected));
    });
    render();
  }));
  $('#file-input').addEventListener('change', event => { uploadFiles([...event.target.files]); event.target.value = ''; });
  let dragDepth = 0;
  $('#dropzone').addEventListener('dragenter', event => { event.preventDefault(); dragDepth++; $('#dropzone').classList.add('drag-over'); });
  $('#dropzone').addEventListener('dragover', event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; });
  $('#dropzone').addEventListener('dragleave', event => { event.preventDefault(); dragDepth--; if (dragDepth <= 0) { dragDepth = 0; $('#dropzone').classList.remove('drag-over'); } });
  $('#dropzone').addEventListener('drop', event => { event.preventDefault(); dragDepth = 0; $('#dropzone').classList.remove('drag-over'); uploadFiles([...event.dataTransfer.files]); });
  window.addEventListener('dragover', event => event.preventDefault());
  window.addEventListener('drop', event => event.preventDefault());
  document.querySelectorAll('[data-format]').forEach(node => node.addEventListener('click', () => {
    state.format = node.dataset.format;
    document.querySelectorAll('[data-format]').forEach(item => { const selected = item.dataset.format === state.format; item.classList.toggle('selected', selected); item.setAttribute('aria-pressed', String(selected)); });
    render();
  }));
  $('#convert-button').addEventListener('click', () => startJobs(state.jobs.filter(job => job.status === 'queued').map(job => job.id)));
  $('#cancel-all-button').addEventListener('click', () => cancelJobs(state.jobs.filter(job => job.status === 'queued' || job.status === 'running').map(job => job.id)));
  $('#clear-button').addEventListener('click', async () => {
    closeDismissHint();
    for (const job of state.jobs.filter(item => item.status !== 'running')) {
      try { await post(`/api/jobs/${encodeURIComponent(job.id)}/hide`, {}); }
      catch (error) { toast(error.message); break; }
    }
    await refresh();
  });
  $('#reconnect-button').addEventListener('click', refresh);
  $('#update-button').addEventListener('click', checkOrOfferUpdate);
  $('#update-cancel').addEventListener('click', () => $('#update-dialog').close());
  $('#update-confirm').addEventListener('click', installUpdate);
  $('#update-dialog').addEventListener('close', () => $('#update-button').focus({ preventScroll: true }));
  $('#download-all').addEventListener('click', saveAllResults);
  $('#installed-button').addEventListener('click', openInstalled);
  $('#output-change').addEventListener('click', openOutputSettings);
  $('#output-form').addEventListener('submit', saveOutputSettings);
  $('#output-cancel').addEventListener('click', () => $('#output-dialog').close());
  $('#output-browse').addEventListener('click', browseOutputDirectory);
  $('#output-dialog').addEventListener('close', () => $('#output-change').focus({preventScroll: true}));
  $('#installed-refresh').addEventListener('click', () => loadInstalled(true));
  $('#duplicate-cancel').addEventListener('click', () => finishDuplicate('cancel'));
  $('#duplicate-replace').addEventListener('click', () => finishDuplicate('replace'));
  $('#duplicate-reuse').addEventListener('click', () => finishDuplicate('reuse'));
  $('#duplicate-dialog').addEventListener('close', () => { if (!$('#duplicate-dialog').open && duplicateDecision) finishDuplicate('cancel'); });
  $('#installed-search').addEventListener('input', event => { state.installedSearch = event.target.value; renderInstalled(); });
  $('#delete-cancel').addEventListener('click', () => $('#delete-dialog').close());
  $('#delete-confirm').addEventListener('click', () => {
    if (!deleteConfirmation) return;
    deleteConfirmation.resolve(true);
    deleteConfirmation.resolve = () => {};
    $('#delete-dialog').close();
  });
  $('#delete-dialog').addEventListener('close', () => {
    const pending = deleteConfirmation;
    deleteConfirmation = null;
    if (pending) { pending.resolve(false); if (pending.trigger?.isConnected) pending.trigger.focus({ preventScroll: true }); }
  });
  $('#close-installed').addEventListener('click', () => $('#installed-dialog').close());
  $('#installed-dialog').addEventListener('close', () => { state.installedToken++; $('#installed-button').focus({ preventScroll: true }); });
  $('#close-log').addEventListener('click', () => $('#log-dialog').close());
  $('#log-dialog').addEventListener('close', () => {
    state.logToken++;
    state.logContent = null;
    state.logRecord = null;
    $('#log-content').textContent = '';
    $('#log-copy-fallback').value = '';
    if (state.logTrigger?.isConnected) state.logTrigger.focus({ preventScroll: true });
    else if ($('#installed-dialog').open) $('#installed-refresh').focus({ preventScroll: true });
  });
  $('#copy-log-button').addEventListener('click', () => {
    if (state.logRecord && state.logContent !== null) copyLogContent(state.logRecord, state.logContent);
  });
  document.addEventListener('keydown', event => {
    if (activeDismissHint && event.key === 'Escape') closeDismissHint();
  }, true);
  document.addEventListener('pointerdown', event => { if (activePageMenu && !activePageMenu.wrapper.contains(event.target)) closePageMenu(); });
  document.addEventListener('pointerdown', event => { document.querySelectorAll('.job-more[open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; }); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') document.querySelectorAll('.job-more[open]').forEach(menu => { menu.open = false; menu.querySelector('summary')?.focus(); }); });
  document.addEventListener('focusin', event => { if (activePageMenu && !activePageMenu.wrapper.contains(event.target)) closePageMenu(); });
  window.addEventListener('resize', () => { closeDismissHint(); closePageMenu(); document.querySelectorAll('.job-more[open]').forEach(menu => { menu.open = false; }); });
  document.addEventListener('scroll', event => { closeDismissHint(); if (activePageMenu && !activePageMenu.menu.contains(event.target)) closePageMenu(); document.querySelectorAll('.job-more[open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; }); }, true);
  render();
  refresh();
  refreshUpdates();
})();
