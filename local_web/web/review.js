(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const dialog = $('#review-dialog');
  if (!dialog) return;
  const state = { token: 0, jobId: null, name: '', review: null, index: 0, busy: false, trigger: null, reportTimer: null, reportId: null };
  const submissions = new Map();
  const decisions = new Set(['error', 'correct']);
  const isDelivered = report => ['sent', 'accepted_by_telegram_transport'].includes(report.status);
  const count = (value, forms) => `${value} ${forms[value % 100 >= 11 && value % 100 <= 14 ? 2 : value % 10 === 1 ? 0 : value % 10 >= 2 && value % 10 <= 4 ? 1 : 2]}`;
  async function request(path, payload) {
    const options = payload === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
    options.headers = { 'X-Local-App': '1', ...(options.headers || {}) };
    options.cache = 'no-store';
    const response = await fetch(path, options);
    let data;
    try { data = await response.json(); } catch { throw new Error('Приложение не вернуло ответ. Повторите попытку.'); }
    if (!response.ok) { const error = new Error(data.error || data.message || 'Не удалось выполнить действие.'); error.status = response.status; throw error; }
    return data;
  }
  function endpoint(suffix = '') { return `/api/jobs/${encodeURIComponent(state.jobId)}/${suffix}`; }
  function unwrap(payload) {
    const review = payload.review || payload;
    if (!review || !Array.isArray(review.items) || typeof review.revision !== 'string') throw new Error('Фрагменты пока недоступны.');
    return review;
  }
  function items() { return state.review?.items || []; }
  function totals() {
    const all = items();
    return { total: all.length, reviewed: all.filter(item => decisions.has(item.decision)).length, errors: all.filter(item => item.decision === 'error').length };
  }
  function complaintSignature() {
    return JSON.stringify([state.jobId, state.review?.revision, items().filter(item => item.decision === 'error').map(item => item.id).sort(), $('#review-comment').value.trim()]);
  }
  function feedback(text = '', error = false) {
    const node = $('#review-feedback'); node.textContent = text; node.hidden = !text; node.classList.toggle('error', error);
  }
  function setBusy(value) {
    state.busy = value;
    $('#review-mark-error').disabled = value;
    $('#review-mark-correct').disabled = value;
    $('#review-send').disabled = value;
    $('#review-prev').disabled = value || state.index <= 0;
    $('#review-next').disabled = value || state.index >= items().length - 1;
    $('#review-summary-button').disabled = value;
    $('#review-return').disabled = value;
    $('#review-complaint-button').disabled = value;
  }
  function showSummary() {
    const { total, reviewed, errors } = totals();
    $('#review-loading').hidden = true;
    $('#review-workflow').hidden = true;
    $('#review-summary').hidden = false;
    $('#review-complaint').hidden = true;
    $('#review-return').hidden = total === 0;
    $('#review-summary-title').textContent = total ? 'Итоги просмотра' : 'Нет фрагментов для сравнения';
    $('#review-summary-text').textContent = total
      ? `Просмотрено ${reviewed} из ${total}. ${errors ? `Вы отметили ${count(errors, ['ошибку', 'ошибки', 'ошибок'])}.` : 'Вы пока не отметили ошибок.'}`
      : 'Для этого документа не найдены фрагменты, требующие сравнения. Если вы заметили ошибку в результате, сообщите об этом разработчику.';
    $('#review-complaint-button').textContent = errors ? `Отправить жалобу · ${errors}` : 'Сообщить об ошибке';
    $('#review-keep').textContent = 'Оставить как есть';
    setBusy(state.busy);
    $('#review-summary-title').focus({ preventScroll: true });
  }
  function pageText(item) {
    const pages = Array.isArray(item.pages) ? [...new Set(item.pages.filter(page => Number.isInteger(page) && page > 0))] : [];
    if (pages.length) return `${pages.length === 1 ? 'Страница' : 'Страницы'} ${pages.join(', ')}`;
    return Number.isInteger(item.page) && item.page > 0 ? `Страница ${item.page}` : '';
  }
  function renderItem() {
    const all = items();
    if (!all.length) return showSummary();
    state.index = Math.max(0, Math.min(state.index, all.length - 1));
    const item = all[state.index];
    const { total, reviewed, errors } = totals();
    $('#review-loading').hidden = true; $('#review-summary').hidden = true; $('#review-complaint').hidden = true; $('#review-workflow').hidden = false;
    $('#review-position').textContent = `Фрагмент ${state.index + 1} из ${total}`;
    $('#review-pages').textContent = pageText(item);
    $('#review-progress-label').textContent = `Просмотрено ${reviewed} из ${total}${errors ? ` · Отмечено ошибок: ${errors}` : ''}`;
    $('#review-progress').max = Math.max(1, total); $('#review-progress').value = reviewed;
    $('#review-current-decision').textContent = item.decision === 'error'
      ? 'Отмечено: есть ошибка. Фрагмент будет включён в жалобу.'
      : item.decision === 'correct' ? 'Отмечено: всё верно. Решение можно изменить.' : 'Выберите решение. Отметка сохранится и откроется следующий фрагмент.';
    $('#review-mark-error').classList.toggle('selected', item.decision === 'error');
    $('#review-mark-correct').classList.toggle('selected', item.decision === 'correct');
    $('#review-mark-error').setAttribute('aria-pressed', String(item.decision === 'error'));
    $('#review-mark-correct').setAttribute('aria-pressed', String(item.decision === 'correct'));
    const base = endpoint(`review/${encodeURIComponent(item.id)}`);
    const revision = `?revision=${encodeURIComponent(state.review.revision)}`;
    const images = $('#review-source-images'); images.replaceChildren(); images.classList.remove('is-zoomed'); images.scrollTop = 0;
    const regionCount = Number.isInteger(item.source_regions_count) ? item.source_regions_count : item.source_available ? 1 : 0;
    const hasSource = item.source_available !== false && regionCount > 0;
    images.hidden = !hasSource; $('#review-source-missing').hidden = hasSource; $('#review-open-original').hidden = hasSource;
    if (hasSource) {
      const imageToken = state.token;
      for (let region = 0; region < regionCount; region++) {
        const figure = document.createElement('figure');
        const zoom = document.createElement('button'); zoom.type = 'button'; zoom.setAttribute('aria-label', 'Увеличить или уменьшить снимок оригинала');
        zoom.addEventListener('click', () => images.classList.toggle('is-zoomed'));
        const image = document.createElement('img'); image.alt = `Полный снимок исходного фрагмента${regionCount > 1 ? `, часть ${region + 1} из ${regionCount}` : ''}`;
        image.src = `${base}/source/${region}.png${revision}`;
        image.addEventListener('error', () => {
          if (imageToken !== state.token || items()[state.index]?.id !== item.id) return;
          const message = document.createElement('p'); message.className = 'review-missing'; message.textContent = 'Снимок этой части недоступен. Можно открыть исходный документ.';
          figure.replaceChildren(message); $('#review-open-original').hidden = false;
        });
        zoom.append(image); figure.append(zoom);
        if (regionCount > 1) { const caption = document.createElement('figcaption'); caption.textContent = `Часть ${region + 1} из ${regionCount}`; figure.append(caption); }
        images.append(figure);
      }
    }
    loadResult(item, `${base}/result.html${revision}`, state.token, state.review.revision);
    setBusy(state.busy);
    $('#review-position').focus({ preventScroll: true });
  }
  async function loadResult(item, url, token, revision) {
    const frame = $('#review-result-frame'); const missing = $('#review-result-missing');
    const scope = ['page', 'document'].includes(item.result_scope) ? item.result_scope : 'fragment';
    const notice = $('#review-result-notice');
    $('#review-result-scope').textContent = scope === 'document' ? 'HTML · весь документ' : scope === 'page' ? 'HTML · страница' : 'HTML';
    notice.textContent = typeof item.result_notice === 'string' && item.result_notice.trim()
      ? item.result_notice.slice(0, 600) : scope === 'document' ? 'Показан весь результат: точные границы фрагмента не определены.' : scope === 'page' ? 'Показана страница результата: точные границы фрагмента не определены.' : '';
    notice.hidden = true;
    frame.title = scope === 'document' ? 'Полный результат конвертации документа' : scope === 'page' ? 'Результат конвертации исходной страницы' : 'Результат конвертации выбранного фрагмента';
    frame.hidden = true; frame.removeAttribute('srcdoc'); frame.src = 'about:blank'; missing.hidden = false;
    missing.textContent = item.result_available === false ? 'Представление этого фрагмента недоступно. Вы можете сообщить об ошибке в документе.' : 'Загружаем результат…';
    if (item.result_available === false) return;
    try {
      const response = await fetch(url, { headers: { 'X-Local-App': '1' }, cache: 'no-store' });
      if (!response.ok) throw new Error('Result unavailable');
      const html = await response.text();
      if (token !== state.token || !dialog.open || items()[state.index]?.id !== item.id || state.review?.revision !== revision) return;
      // Only the server's sanitized, script-free fragment enters this sandbox.
      if (item.result_anchor === 'region-review-target') frame.src = `${url}#region-review-target`;
      else frame.srcdoc = html;
      frame.hidden = false; missing.hidden = true; notice.hidden = scope === 'fragment';
    } catch {
      if (token !== state.token || !dialog.open || items()[state.index]?.id !== item.id || state.review?.revision !== revision) return;
      missing.textContent = 'Не удалось загрузить этот фрагмент результата. Вернитесь к нему ещё раз или сообщите об ошибке.';
    }
  }
  async function loadReview(token, preferSummary = false) {
    try {
      const data = unwrap(await request(endpoint('review')));
      if (token !== state.token || !dialog.open) return;
      state.review = data;
      if (preferSummary || items().length && totals().reviewed === items().length) showSummary();
      else {
        const next = items().findIndex(item => !decisions.has(item.decision));
        state.index = next < 0 ? 0 : next; renderItem();
      }
    } catch (error) {
      if (token !== state.token || !dialog.open) return;
      $('#review-loading').hidden = false; $('#review-loading').className = 'review-message error';
      $('#review-loading').textContent = error.status === 404 ? 'Для этого документа проверка пока недоступна. Закройте окно и откройте его снова после обработки.' : 'Не удалось загрузить фрагменты. Закройте окно и попробуйте снова.';
    }
  }
  async function open(jobId, name) {
    if (typeof jobId !== 'string' || !jobId) return;
    state.token++; clearTimeout(state.reportTimer);
    state.jobId = jobId; state.name = name || 'Документ'; state.review = null; state.index = 0; state.busy = false; state.reportId = null;
    state.trigger = document.activeElement;
    $('#review-file').textContent = state.name; $('#review-comment').value = ''; $('#review-comment').readOnly = false; $('#review-send').textContent = 'Отправить жалобу';
    $('#review-loading').hidden = false; $('#review-loading').className = 'review-message'; $('#review-loading').textContent = 'Загрузка фрагментов…';
    $('#review-workflow').hidden = true; $('#review-summary').hidden = true; $('#review-complaint').hidden = true; feedback();
    if (!dialog.open) dialog.showModal();
    $('#review-close').focus({ preventScroll: true }); dialog.scrollTop = 0;
    await loadReview(state.token);
  }
  async function decide(decision) {
    if (state.busy || !state.review || !items()[state.index]) return;
    const token = state.token; const currentId = items()[state.index].id; const oldIndex = state.index;
    setBusy(true); feedback();
    try {
      const data = unwrap(await request(endpoint('review'), { item_id: currentId, decision, revision: state.review.revision }));
      if (token !== state.token || !dialog.open) return;
      state.review = data;
      const all = items();
      const order = all.map((_, index) => (oldIndex + 1 + index) % all.length);
      const next = order.find(index => !decisions.has(all[index].decision));
      if (next === undefined) showSummary(); else { state.index = next; renderItem(); }
    } catch (error) {
      if (token !== state.token || !dialog.open) return;
      if (error.status === 409) {
        feedback('Результат документа обновился. Список фрагментов загружен заново.');
        await loadReview(token);
      } else feedback('Не удалось сохранить отметку. Повторите выбор.', true);
    } finally { if (token === state.token && dialog.open) setBusy(false); }
  }
  function showComplaint() {
    if (state.busy || !state.review) return;
    const { errors } = totals();
    $('#review-workflow').hidden = true; $('#review-summary').hidden = true; $('#review-complaint').hidden = false;
    $('#review-complaint-context').textContent = errors
      ? `В жалобу войдут ${count(errors, ['отмеченный фрагмент', 'отмеченных фрагмента', 'отмеченных фрагментов'])} и сведения о документе.`
      : 'Сообщите об ошибке в документе. Приложение приложит доступные материалы автоматически.';
    syncComplaint();
    $('#review-comment').focus();
    $('#review-complaint').scrollIntoView({ block: 'start' });
  }
  function deliveryText(report) {
    if (report.delivery_channel === 'previous' && isDelivered(report)) return 'Жалоба отправлена ранее.';
    if (isDelivered(report)) return 'Жалоба отправлена разработчику в Telegram. Спасибо за сообщение.';
    if (report.status === 'failed') return report.next_attempt_at == null
      ? 'Жалоба сохранена, но подготовить отчёт не удалось. Повторите отправку.'
      : 'Жалоба сохранена, но отправить её в Telegram пока не удалось. Приложение повторит попытку автоматически.';
    if (report.status === 'sending') return 'Жалоба сохранена. Отправляем разработчику в Telegram…';
    if (['queued', 'preparing'].includes(report.status)) return 'Жалоба сохранена и ожидает отправки. Можно продолжать работу с документами.';
    return 'Жалоба сохранена. Статус отправки пока недоступен.';
  }
  function syncComplaint() {
    clearTimeout(state.reportTimer);
    const signature = complaintSignature(); const token = state.token;
    const entry = submissions.get(signature);
    const report = entry?.report;
    const retryPreparation = report?.status === 'failed' && report.next_attempt_at == null;
    state.reportId = report?.id || null;
    $('#review-send').disabled = state.busy || !!report && !retryPreparation;
    $('#review-send').textContent = retryPreparation ? 'Повторить отправку' : report ? isDelivered(report) ? 'Жалоба отправлена' : 'Жалоба сохранена' : 'Отправить жалобу';
    feedback(report ? deliveryText(report) : '');
    if (report && !retryPreparation && !isDelivered(report)) state.reportTimer = setTimeout(() => pollReport(token, signature, report.id), 4000);
  }
  async function pollReport(token, signature, reportId, attempt = 0) {
    if (token !== state.token || state.reportId !== reportId || !dialog.open || attempt >= 30) return;
    try {
      const payload = await request(`/api/reports/${encodeURIComponent(reportId)}`);
      if (token !== state.token || state.reportId !== reportId || !dialog.open) return;
      const report = payload.report || payload;
      const entry = submissions.get(signature); if (entry) entry.report = report;
      if (!$('#review-complaint').hidden && complaintSignature() === signature) {
        feedback(deliveryText(report), false);
        if (isDelivered(report)) $('#review-send').textContent = 'Жалоба отправлена';
        if (report.status === 'failed' && report.next_attempt_at == null) { syncComplaint(); return; }
      }
      if (isDelivered(report)) return;
    } catch { /* The durable complaint remains queued when delivery status cannot be refreshed. */ }
    state.reportTimer = setTimeout(() => pollReport(token, signature, reportId, attempt + 1), 4000);
  }
  async function sendComplaint(event) {
    event.preventDefault();
    if (state.busy || !state.review) return;
    const token = state.token;
    const signature = complaintSignature();
    let entry = submissions.get(signature);
    if (entry?.report && !(entry.report.status === 'failed' && entry.report.next_attempt_at == null)) { syncComplaint(); return; }
    if (!entry) { entry = { key: globalThis.crypto?.randomUUID?.() || `complaint-${Date.now()}-${Math.random().toString(16).slice(2)}` }; submissions.set(signature, entry); }
    setBusy(true); $('#review-send').textContent = 'Сохраняем жалобу…'; feedback();
    $('#review-comment').readOnly = true;
    try {
      const payload = await request(endpoint('complaints'), { revision: state.review.revision, comment: $('#review-comment').value.trim(), idempotency_key: entry.key });
      if (token !== state.token || !dialog.open) return;
      const report = payload.report || payload;
      if (!report.id) throw new Error('Нет подтверждения сохранения жалобы.');
      entry.report = report; state.reportId = report.id;
      feedback(deliveryText(report)); $('#review-send').textContent = report.status === 'failed' && report.next_attempt_at == null ? 'Повторить отправку' : isDelivered(report) ? 'Жалоба отправлена' : 'Жалоба сохранена';
      if (!isDelivered(report) && !(report.status === 'failed' && report.next_attempt_at == null)) state.reportTimer = setTimeout(() => pollReport(token, signature, report.id), 4000);
    } catch (error) {
      if (token !== state.token || !dialog.open) return;
      $('#review-send').textContent = 'Повторить отправку';
      feedback(error.status === 409 ? 'Результат документа обновился. Закройте окно и откройте проверку заново, чтобы отправить актуальные отметки.' : 'Не удалось сохранить жалобу. Повторите попытку.', true);
    } finally {
      if (token === state.token && dialog.open) { setBusy(false); $('#review-comment').readOnly = false; $('#review-send').disabled = !!entry.report && !(entry.report.status === 'failed' && entry.report.next_attempt_at == null); }
    }
  }
  $('#review-close').addEventListener('click', () => dialog.close());
  $('#review-keep').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    state.token++; clearTimeout(state.reportTimer); $('#review-result-frame').removeAttribute('srcdoc'); $('#review-result-frame').src = 'about:blank'; $('#review-comment').readOnly = false;
    const row = [...document.querySelectorAll('.job-row')].find(node => node.dataset.id === state.jobId);
    const trigger = state.trigger?.isConnected ? state.trigger : row?.querySelector('.review-job-button') || $('#add-files-button');
    trigger?.focus({ preventScroll: true });
  });
  $('#review-mark-error').addEventListener('click', () => decide('error'));
  $('#review-mark-correct').addEventListener('click', () => decide('correct'));
  $('#review-prev').addEventListener('click', () => { if (!state.busy && state.index > 0) { state.index--; renderItem(); } });
  $('#review-next').addEventListener('click', () => { if (!state.busy && state.index < items().length - 1) { state.index++; renderItem(); } });
  $('#review-summary-button').addEventListener('click', showSummary);
  $('#review-return').addEventListener('click', renderItem);
  $('#review-complaint-button').addEventListener('click', showComplaint);
  $('#review-complaint-cancel').addEventListener('click', () => { if (!state.busy) showSummary(); });
  $('#review-complaint').addEventListener('submit', sendComplaint);
  $('#review-comment').addEventListener('input', () => { if (!state.busy) syncComplaint(); });
  $('#review-open-original').addEventListener('click', async () => {
    const token = state.token;
    try { await request(endpoint('open'), { target: 'source' }); } catch { if (token === state.token && dialog.open) feedback('Не удалось открыть оригинал. Попробуйте открыть его из списка документов.', true); }
  });
  [['review-tab-original', 'original'], ['review-tab-result', 'result']].forEach(([id, view]) => {
    $(`#${id}`).addEventListener('click', () => {
      $('#review-comparison').dataset.mobileView = view;
      $('#review-tab-original').classList.toggle('selected', view === 'original'); $('#review-tab-original').setAttribute('aria-pressed', String(view === 'original'));
      $('#review-tab-result').classList.toggle('selected', view === 'result'); $('#review-tab-result').setAttribute('aria-pressed', String(view === 'result'));
    });
  });
  window.RegionReview = Object.freeze({ open });
})();
