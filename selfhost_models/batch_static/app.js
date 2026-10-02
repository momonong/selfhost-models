'use strict';
const $ = (id) => document.getElementById(id);
const state = {
  pane: 'work', bootstrap: null, projects: [], environments: [], locations: [],
  drafts: [], jobs: [], legacy: [], project: null, draft: null, job: null,
  folder: null, dirtyProject: false, dirtyDraft: false, busy: false
};
const fields = ['title', 'partition', 'gpus', 'minutes', 'cpus', 'memory_gib', 'project_type', 'max_cost_twd', 'account', 'email'];
const numbers = new Set(['gpus', 'minutes', 'cpus', 'memory_gib', 'max_cost_twd']);
const workNames = {prepared: '已準備，尚未提交', running: '執行中', unknown: '結果不明', succeeded: '工作完成', failed: '工作失敗', canceled: '已確認取消'};
const resultNames = {none: '尚無結果', pending: '結果待收集', available: '結果可下載'};
const labels = {title: '工作名稱', entrypoint: '程式入口', arguments: '程式參數', minutes: '時間上限', gpus: 'GPU 數量', max_cost_twd: 'GPU 費用上限', account: '計畫 ID', email: '通知 email', resources: '資源與費用設定', name: '名稱', working_dir: '工作目錄'};
const presets = {short: ['dev', 20], training: ['normal', 60], large: ['normal2', 60]};
function el(tag, text, cls) {
  const item = document.createElement(tag);
  if (text !== undefined && text !== null) item.textContent = String(text);
  if (cls) item.className = cls;
  return item;
}
function button(text, handler, cls = 'button secondary') {
  const item = el('button', text, cls); item.type = 'button'; item.addEventListener('click', handler); return item;
}
function link(text, path, cls = 'button secondary') {
  const item = el('a', text, cls); item.href = path; item.setAttribute('download', ''); return item;
}
function bytes(n) { return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 1048576).toFixed(1)} MiB`; }
function when(v) { const d = new Date(typeof v === 'number' ? v * 1000 : v); return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('zh-TW', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}); }
function lines(id) { return $(id).value.split('\n').filter((line) => line.length > 0); }
function error(message, fieldErrors = {}) { const e = new Error(message); e.fields = fieldErrors; return e; }
function showError(e) {
  $('error-banner').replaceChildren(el('strong', '這一步尚未完成'), el('p', e.message || '操作失敗，請保留輸入後重試。'));
  for (const [field, message] of Object.entries(e.fields || {})) $('error-banner').append(el('p', `${labels[field] || field}：${message}`, 'hint'));
  $('error-banner').hidden = false; $('error-banner').focus();
}
function notice(text) { $('notice').textContent = text; $('notice').hidden = false; }
async function api(path, method = 'GET', body) {
  const options = {method, credentials: 'same-origin', headers: {Accept: 'application/json'}};
  if (method !== 'GET') { options.headers['Content-Type'] = 'application/json'; options.headers['X-Batch-CSRF'] = state.bootstrap.csrf_token; options.body = JSON.stringify(body || {}); }
  let response, data;
  try { response = await fetch(path, options); } catch { throw error('無法連上本機工作室。請確認服務仍在執行；你的輸入已保留。'); }
  try { data = await response.json(); } catch { throw error('本機服務回傳了無法讀取的內容。'); }
  if (!response.ok) throw error(data.error?.message || `操作失敗（${response.status}）`, data.error?.fields);
  return data;
}
async function act(callback) {
  if (state.busy) return;
  state.busy = true; $('main').setAttribute('aria-busy', 'true'); $('error-banner').hidden = true; $('notice').hidden = true;
  const controls = [...document.querySelectorAll('button, input, select, textarea')];
  const disabled = controls.map((item) => item.disabled);
  controls.forEach((item) => { item.disabled = true; });
  try { await callback(); } catch (e) { showError(e); }
  finally {
    controls.forEach((item, i) => { if (item.isConnected) item.disabled = disabled[i]; });
    state.busy = false; $('main').removeAttribute('aria-busy');
  }
}
function leaving() {
  return !(state.dirtyProject || state.dirtyDraft || state.folder) || window.confirm('有未保存設定或未匯入的資料夾。離開會放棄這些變更，仍要繼續嗎？');
}
function pane(name) {
  state.pane = name;
  for (const current of ['work', 'projects', 'demo']) {
    $(`${current}-pane`).hidden = current !== name; $(`tab-${current}`).classList.toggle('active', current === name);
    if (current === name) $(`tab-${current}`).setAttribute('aria-current', 'page'); else $(`tab-${current}`).removeAttribute('aria-current');
  }
}
function valid(form) {
  const field = form.querySelector(':invalid');
  if (!field) return true;
  field.closest('details')?.setAttribute('open', '');
  const label = form.querySelector(`label[for="${field.id}"]`)?.textContent || '欄位';
  showError(error(`${label}尚未填妥，請確認必填、格式與允許的數值範圍。`)); return false;
}
function replace(list, item) { const index = list.findIndex((old) => old.id === item.id); if (index < 0) list.unshift(item); else list[index] = item; }
function options(select, items, selected = '', empty = '請選擇') {
  select.replaceChildren(); const first = el('option', empty); first.value = ''; select.append(first);
  for (const item of items) { const option = el('option', item.name); option.value = item.id; select.append(option); }
  select.value = selected || '';
}
function fileList(target, files) {
  target.replaceChildren();
  for (const file of files) { const row = el('li', null, 'file-row'); row.append(el('strong', file.path), el('span', file.reason || bytes(file.size), 'muted')); target.append(row); }
}
function checkboxes(target, selected) {
  target.replaceChildren();
  if (!state.locations.length) target.append(el('p', '尚未登錄資料位置，可以先留空完成離線準備。', 'hint'));
  for (const location of state.locations) {
    const label = el('label', null, 'checkbox-label'); const input = el('input'); input.type = 'checkbox'; input.value = location.id; input.checked = selected.includes(location.id);
    label.append(input, el('span', `${location.name} · ${location.kind === 'model' ? '模型' : '資料集'} · 未核對`)); target.append(label);
  }
}
function checked(id) { return [...$(id).querySelectorAll('input:checked')].map((item) => item.value); }
function snapshot(target, data) {
  target.replaceChildren();
  if (!data) { target.append(el('p', '舊版紀錄沒有專案來源。', 'hint')); return; }
  target.append(el('strong', `${data.project_name} · 版本 ${data.project_revision}`), el('p', `入口 ${data.entrypoint} · 工作目錄 ${data.working_dir || '專案根目錄'}`, 'hint'));
  const env = data.environment;
  target.append(el('p', `環境：${env?.name || '待核對環境'}${env ? '（未核對）' : ''} · 資料位置 ${(data.locations || []).map((item) => item.name).join('、') || '未設定'}`, 'hint'));
}
function rememberProject(project) { replace(state.projects, project); state.project = project; state.dirtyProject = false; renderProjects(); }
function renderProjects() {
  $('project-count').textContent = state.projects.length; $('project-list').replaceChildren();
  options($('work-project'), state.projects.map((p) => ({id: p.id, name: `${p.config.name} · 版本 ${p.revision}`})), state.project?.id, '選擇已登錄專案');
  for (const project of state.projects) {
    const item = button('', () => act(async () => { if (!leaving()) return; state.folder = null; rememberProject(await api(`/api/projects/${project.id}`)); }), 'record-item');
    item.classList.toggle('selected', state.project?.id === project.id); item.append(el('strong', project.config.name), el('span', `版本 ${project.revision} · ${project.files.length} 檔`, 'muted')); $('project-list').append(item);
  }
  $('project-empty').hidden = Boolean(state.project); $('project-form').hidden = !state.project;
  if (!state.project) return;
  const p = state.project, c = p.config;
  $('project-title').textContent = c.name; $('project-version').textContent = `程式專案 · 版本 ${p.revision}`; $('project-save-state').textContent = '已保存';
  $('project-name').value = c.name; $('project-working-dir').value = c.working_dir || ''; $('project-arguments').value = (c.arguments || []).join('\n');
  const python = p.files.filter((file) => file.path.toLowerCase().endsWith('.py')).map((file) => ({id: file.path, name: file.path}));
  options($('project-entrypoint'), python, c.entrypoint, python.length ? '選擇程式入口' : '請先匯入程式資料夾');
  if (!c.entrypoint && python.length === 1) $('project-entrypoint').value = python[0].id;
  options($('project-environment'), state.environments, c.environment_id, '待核對環境'); checkboxes($('project-locations'), c.location_ids || []);
  $('project-file-summary').textContent = `${p.files.length} 個檔案 · ${bytes(p.files.reduce((sum, file) => sum + file.size, 0))}`; fileList($('project-files'), p.files);
  $('server-exclusions').replaceChildren();
  if (p.exclusions?.length) {
    const details = el('details'); details.append(el('summary', `服務端排除項目（${p.exclusions.length}）`));
    const list = el('ul', null, 'file-list'); fileList(list, p.exclusions.map((v) => typeof v === 'string' ? {path: v, reason: '服務端安全排除'} : v)); details.append(list); $('server-exclusions').append(details);
  }
  renderFolder();
}
function projectConfig() {
  return {
    name: $('project-name').value.trim(), working_dir: $('project-working-dir').value.trim(),
    entrypoint: $('project-entrypoint').value, arguments: lines('project-arguments'),
    environment_id: $('project-environment').value || null, location_ids: checked('project-locations')
  };
}
async function saveProject() {
  const saved = await api(`/api/projects/${state.project.id}`, 'PUT', {revision: state.project.revision, config: projectConfig()}); rememberProject(saved); return saved;
}
function exclusion(path) {
  const parts = path.toLowerCase().split('/'), name = parts.at(-1);
  if (parts.some((part) => ['.git', '.hg', '.svn', '.venv', 'venv', '__pycache__', '.cache', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules'].includes(part))) return '版本庫、虛擬環境或快取';
  if (parts.some((part) => ['.ssh', 'keys', '.aws', '.azure', '.gnupg', '.codex', '.agents'].includes(part))) return '憑證或私密設定目錄';
  if (parts.some((part) => part.startsWith('.env')) || /\.(pem|key|p12|pfx|jks|keystore)$/.test(name)) return '環境秘密或金鑰檔案';
  // These are excluded file basenames, not authentication values.
  const excludedNames = [
    'credentials',
    'secrets',
    'token',
    'id_rsa',
    'id_ed25519',
    'id_ecdsa',
    'password',
  ];
  if (excludedNames.includes(name.split('.')[0])) return '可能含認證資訊的檔案';
  if (name.endsWith('.pyc') || name.endsWith('.pyo')) return 'Python 編譯快取';
  return '';
}
function pathProblem(path) {
  const parts = path.split('/'), encoder = new TextEncoder();
  return !path || encoder.encode(path).length > 1024 || path !== path.normalize('NFC') || path.startsWith('/') || /[\\:\u0000-\u001f\u007f\p{Cf}]/u.test(path) || parts.some((part) => !part || ['.', '..'].includes(part) || part.startsWith('-') || encoder.encode(part).length > 240);
}
function selectFolder() {
  const files = [...$('project-folder').files]; if (!files.length) return;
  if (files.length > 2000) { state.folder = null; renderFolder(); showError(error('一次最多選取 2000 個項目。請選擇較小的程式資料夾，移除大型環境與資料副本。')); return; }
  const paths = files.map((file) => file.webkitRelativePath || file.name), root = paths[0].split('/')[0];
  const strip = paths.every((path) => path.includes('/') && path.split('/')[0] === root);
  const included = [], excluded = [];
  for (let i = 0; i < files.length; i++) {
    const file = files[i], path = strip ? paths[i].slice(root.length + 1) : paths[i];
    if (pathProblem(path)) { state.folder = null; renderFolder(); showError(error(`檔案路徑不符合安全限制：${path}。請檢查階層、名稱與 Unicode 格式。`)); return; }
    const reason = exclusion(path);
    (reason ? excluded : included).push({path, size: file.size, file, reason});
  }
  state.folder = {included, excluded}; renderFolder();
}
function renderFolder() {
  $('folder-review').hidden = !state.folder;
  if (!state.folder) { $('project-folder').value = ''; return; }
  const {included, excluded} = state.folder;
  $('folder-summary').textContent = `將匯入 ${included.length} 檔 · ${bytes(included.reduce((n, file) => n + file.size, 0))}；排除 ${excluded.length} 檔（未讀取或傳送其內容）`;
  fileList($('folder-included'), included); fileList($('folder-excluded'), excluded);
  $('folder-excluded-summary').textContent = `排除項目與原因（${excluded.length}）`; $('folder-excluded-details').open = excluded.length > 0;
}
async function base64(file) {
  const data = new Uint8Array(await file.arrayBuffer()); let text = '';
  for (let i = 0; i < data.length; i += 8192) text += String.fromCharCode(...data.subarray(i, i + 8192)); return btoa(text);
}
async function importFolder() {
  const folder = state.folder, limits = state.bootstrap.limits;
  if (!folder?.included.length) throw error('沒有可匯入的程式檔案。請檢查選取的資料夾與排除清單。');
  if (folder.included.length > limits.file_count || folder.included.some((f) => f.size > limits.file_bytes) || folder.included.reduce((n, f) => n + f.size, 0) > limits.total_bytes) throw error(`程式快照上限 ${bytes(limits.total_bytes)}、${limits.file_count} 檔；單檔 ${bytes(limits.file_bytes)}。請只選取小型程式專案。`);
  const project = await saveProject(), files = [];
  for (const file of folder.included) files.push({path: file.path, data_base64: await base64(file.file)});
  for (const file of folder.excluded) files.push({path: file.path, data_base64: ''});
  const updated = await api(`/api/projects/${project.id}/files`, 'POST', {revision: project.revision, files});
  state.folder = null; rememberProject(updated); notice('整份程式快照已更新為新版本；舊版本與既有工作保持不變。');
}
function rememberDraft(draft) { replace(state.drafts, draft); state.draft = draft; state.job = null; state.dirtyDraft = false; renderWork(); }
async function newWork(projectId) {
  rememberDraft(await api('/api/work-drafts', 'POST', {project_id: projectId})); $('new-work-panel').hidden = true; pane('work'); notice('已固定專案版本，請設定這次的參數與資源。');
}
function primaryJobs() { return state.jobs.filter((job) => job.origin === 'project' && job.mode !== 'offline_fixture'); }
function renderWork() {
  const jobs = primaryJobs(); $('work-count').textContent = jobs.length + state.drafts.length;
  $('work-empty').hidden = jobs.length + state.drafts.length > 0; $('work-layout').hidden = Boolean(state.draft) || !jobs.length && !state.drafts.length; $('work-form').hidden = !state.draft;
  $('work-empty-title').textContent = state.projects.length ? '從專案建立第一份工作' : '先建立第一個專案';
  $('work-empty-copy').textContent = state.projects.length ? '選擇已登錄專案，程式、環境與資料位置會直接帶入。' : '一次匯入程式資料夾，設定入口與預設參數。之後建立工作不必再逐檔選取。';
  $('empty-action').textContent = state.projects.length ? '新建工作' : '建立專案'; $('work-list').replaceChildren();
  for (const draft of state.drafts) {
    const item = button('', () => act(async () => { if (!leaving()) return; rememberDraft(await api(`/api/work-drafts/${draft.id}`)); }), 'record-item');
    item.append(el('strong', draft.spec.title), el('span', `草稿 · ${draft.snapshot?.project_name || ''}`, 'muted')); $('work-list').append(item);
  }
  for (const job of jobs) $('work-list').append(jobItem(job));
  renderDraft(); if (!state.draft) renderJob($('work-detail'), state.job, false); renderDemo();
}
function renderDraft() {
  const d = state.draft; if (!d) return;
  $('work-save-state').textContent = '已保存'; snapshot($('work-snapshot'), d.snapshot);
  for (const field of fields) $(field).value = d.spec[field] ?? ''; $('arguments').value = (d.spec.arguments || []).join('\n');
  const preset = Object.entries(presets).find(([, [partition, minutes]]) => partition === d.spec.partition && minutes === d.spec.minutes && d.spec.gpus === 1);
  $('resource-preset').value = preset?.[0] || 'custom'; checkboxes($('work-locations'), (d.snapshot.locations || []).map((item) => item.id));
  $('work-environment').textContent = `固定環境：${d.snapshot.environment?.name || '待核對環境'}。要變更環境，請在專案設定中選擇，再建立新工作。`;
  costSummary();
}
function costSummary() {
  if (!state.draft) return;
  const part = state.bootstrap.partitions.find((p) => p.id === $('partition').value), hours = Number($('gpus').value) * Number($('minutes').value) / 60;
  $('minutes').max = part.max_minutes;
  const rates = state.bootstrap.rates[part.gpu], rate = rates[$('project_type').value], amount = hours * (rate ?? Math.max(...Object.values(rates)));
  $('work-cost').textContent = `${part.gpu} · ${hours.toFixed(2)} GPU·hr · ${rate === undefined ? '費率待核對，上限檢查採最高公開費率' : 'GPU 費用估算上限'} NT$ ${amount.toLocaleString('zh-TW', {maximumFractionDigits: 2})}${amount > Number($('max_cost_twd').value) ? '（超出設定的費用上限）' : ''}`;
}
async function saveWork() {
  const d = state.draft, spec = {...d.spec, arguments: lines('arguments')};
  for (const field of fields) spec[field] = numbers.has(field) ? Number($(field).value) : $(field).value.trim();
  const saved = await api(`/api/work-drafts/${d.id}`, 'PUT', {revision: d.revision, spec, location_ids: checked('work-locations')}); rememberDraft(saved); return saved;
}
function jobItem(job, demo = false) {
  const item = button('', () => act(async () => { if (!leaving()) return; state.draft = null; state.dirtyDraft = false; state.job = await api(`/api/jobs/${job.id}`); demo ? renderDemo() : renderWork(); }), 'record-item');
  item.classList.toggle('selected', state.job?.id === job.id);
  item.append(el('strong', job.spec.title), el('span', job.mode === 'offline_fixture' ? `離線示範 · ${workNames[job.work_state]}` : workNames[job.work_state], 'muted'), el('span', resultNames[job.result_state], 'muted')); return item;
}
function card(title, copy) { const box = el('section', null, 'card'); box.append(el('h3', title)); if (copy) box.append(el('p', copy, 'hint')); return box; }
function details(title, ...content) { const box = el('details'); box.append(el('summary', title), ...content); return box; }
async function mutate(endpoint, body = {}) {
  const job = await api(`/api/jobs/${state.job.id}/${endpoint}`, 'POST', body); replace(state.jobs, job); state.job = job; renderWork();
}
function renderJob(target, job, demo) {
  target.replaceChildren();
  if (!job || !demo && (job.origin !== 'project' || job.mode === 'offline_fixture')) { const box = el('div', null, 'empty-state'); box.append(el('h3', demo ? '選擇工作紀錄' : '選擇工作或草稿'), el('p', demo ? '在這裡檢查離線示範與保留的舊版資料。' : '工作狀態、專案版本與結果會顯示在這裡。')); target.append(box); return; }
  const path = `/api/jobs/${job.id}`, heading = el('div', null, 'job-heading'); heading.append(el('p', `工作 ${job.id}`, 'eyebrow'), el('h2', job.spec.title));
  const badges = el('div', null, 'badges'); badges.append(el('span', workNames[job.work_state], `tag state-${job.work_state}`), el('span', resultNames[job.result_state], 'tag')); if (job.mode === 'offline_fixture') badges.append(el('span', '離線 fixture', 'tag subtle')); if (job.origin !== 'project') badges.append(el('span', '舊版紀錄', 'tag subtle')); heading.append(badges); target.append(heading);
  const overview = card('這份工作'); const summary = el('div', null, 'snapshot-summary'); snapshot(summary, job.snapshot); overview.append(summary);
  overview.append(el('p', `${job.spec.partition} · ${job.spec.gpus} GPU · ${job.spec.minutes} 分鐘`, 'resource-strip'));
  overview.append(el('p', `程式參數：${job.spec.arguments.length ? job.spec.arguments.map((argument) => JSON.stringify(argument)).join(' ') : '無'}`, 'hint'));
  const cost = job.cost || {};
  const amount = cost.estimated_max_twd ?? cost.conservative_max_twd;
  overview.append(el('p', `${cost.estimated_max_twd == null ? '費率待核對，最高公開費率估算' : 'GPU 費用估算上限'} NT$ ${amount ?? '待確認'}；設定上限 NT$ ${cost.cap_twd ?? job.spec.max_cost_twd}，不含儲存費。`, 'hint'));
  if (!demo) { const submit = button('正式提交國網（目前停用）', () => {}, 'button primary wide'); submit.disabled = true; overview.append(submit, el('p', '已準備完成，尚未提交國網。正式連線須另階段由你在場時核對。', 'hint')); }
  if (job.snapshot) overview.append(button('複製設定，建立新工作', () => act(async () => { if (!leaving()) return; rememberDraft(await api(`${path}/clone`, 'POST')); pane('work'); notice('已建立新草稿，保留原工作使用的程式版本；沒有重送原工作。'); })));
  target.append(overview);
  if (job.work_state === 'unknown') target.append(el('p', '結果不明已保存，不會自動重送或重試。請保留紀錄，根據後續證據核對。', 'inline-warning'));
  if (job.cancel_requested && ['running', 'unknown'].includes(job.work_state)) target.append(el('p', '取消請求已記錄，尚未確認停止。取消不代表資源已釋放。', 'inline-warning'));
  if (['running', 'unknown'].includes(job.work_state) && !job.cancel_requested) target.append(button('記錄取消請求', () => act(() => mutate('cancel'))));
  if (demo) renderFixture(target, job);
  const results = card('結果', '工作完成與結果可用分開記錄。匯入結果不會確認工作是否完成。');
  const actions = el('div', null, 'actions');
  if (demo && job.mode === 'offline_fixture' && job.work_state === 'succeeded' && job.result_state !== 'available') actions.append(button('收集離線示範結果', () => act(() => mutate('results/collect')), 'button primary'));
  const label = el('label', '匯入本地結果 ZIP', 'button secondary import-label'), input = el('input'); input.type = 'file'; input.accept = '.zip'; input.setAttribute('aria-label', '匯入本地結果 ZIP');
  input.addEventListener('change', () => act(async () => {
    const file = input.files[0];
    if (!file) return;
    if (file.size > state.bootstrap.limits.total_bytes) throw error('結果 ZIP 超過 16 MiB 上限。');
    await mutate('results/import', {data_base64: await base64(file)});
    notice('結果已匯入；保留原工作狀態。');
  }));
  label.append(input); actions.append(label);
  if (job.result_state === 'available') actions.append(link('下載全部結果', `${path}/results/download`, 'button primary')); results.append(actions);
  results.append(el('p', '結果每包最多 16 MiB／100 檔，適用小型離線準備。', 'hint'));
  if (job.result_state === 'available') {
    results.append(el('p', job.result_provenance === 'offline_fixture' ? '來源：固定 CPU 離線 fixture，不代表你的程式產出。' : '來源：手動匯入，本工具未驗證來源或訓練品質。', 'hint'));
    const list = el('ul', null, 'file-list'), preview = el('div');
    for (const file of job.results) {
      const row = el('li', null, 'file-row');
      const open = button(file.path, () => act(async () => {
        const data = await api(`${path}/results/preview?path=${encodeURIComponent(file.path)}`);
        preview.replaceChildren(el('h4', data.path), el('pre', data.text, 'result-text'));
        if (data.truncated) preview.append(el('p', '預覽已截短，請下載完整檔案。', 'hint'));
      }), 'file-link');
      row.append(open, link('下載', `${path}/results/file?path=${encodeURIComponent(file.path)}`, 'text-button'));
      list.append(row);
    }
    results.append(list, preview);
  } else results.append(el('p', resultNames[job.result_state], 'empty-copy')); target.append(results);
  const events = card('狀態紀錄'), list = el('ol', null, 'event-list');
  for (const event of job.events || []) { const item = el('li'); item.append(el('span', when(event.at), 'muted'), el('p', event.message)); list.append(item); } events.append(list); target.append(events);
  const advanced = card('進階檢查'), files = el('ul', null, 'file-list'); fileList(files, job.inputs || []);
  const blockers = el('ul', null, 'checklist'); for (const message of job.blockers || []) blockers.append(el('li', message));
  const packageControls = el('div', null, 'actions'); packageControls.append(link('下載工作包', `${path}/package`));
  advanced.append(details('腳本、工作包與輸入清單', packageControls, el('pre', job.script, 'script-preview'), files), details('真機提交前待核對', blockers)); target.append(advanced);
}
function renderFixture(target, job) {
  const demo = card('固定 CPU 離線示範', '只執行內建 fixture，每份工作最多示範一次；不執行專案程式。');
  if (job.attempt_count === 0 && job.work_state === 'prepared') {
    const select = el('select'); select.id = 'demo-scenario'; const label = el('label', '示範情境'); label.htmlFor = select.id;
    for (const [value, name] of [['success', '完成後等待收集'], ['unknown', '結果不明'], ['cancel', '取消待確認']]) { const option = el('option', name); option.value = value; select.append(option); }
    demo.append(label, select, button('執行一次離線示範', () => act(async () => { await mutate('demo', {scenario: select.value}); notice('離線示範已記錄，此工作已移出主要工作列表。'); }), 'button secondary'));
  } else demo.append(el('p', '已記錄示範意圖，不提供重送。', 'hint'));
  if (job.mode === 'offline_fixture' && ['unknown', 'running'].includes(job.work_state)) {
    const select = el('select'); select.id = 'fixture-outcome'; const label = el('label', '確認 fixture 狀態'); label.htmlFor = select.id;
    for (const [value, name] of [['succeeded', '已完成'], ['canceled', '已確認停止'], ['failed', '已失敗']]) { const option = el('option', name); option.value = value; select.append(option); }
    demo.append(details('僅離線 fixture：依證據確認結果', label, select, button('確認離線狀態', () => act(() => mutate('demo-confirm', {outcome: select.value})))));
  }
  target.append(demo);
}
function renderDemo() {
  $('demo-list').replaceChildren();
  for (const job of state.jobs) $('demo-list').append(jobItem(job, true));
  renderJob($('demo-detail'), state.job, true); $('legacy-drafts').replaceChildren();
  for (const draft of state.legacy) { const row = el('p', `${draft.spec.title} · ${draft.files.length} 檔 · ${when(draft.updated_at)}`, 'hint'); $('legacy-drafts').append(row); }
  if (!state.legacy.length) $('legacy-drafts').append(el('p', '沒有舊版草稿。', 'hint'));
}
async function refresh() {
  const paths = ['/api/projects', '/api/environments', '/api/locations', '/api/work-drafts', '/api/jobs', '/api/drafts'];
  const [projects, environments, locations, drafts, jobs, legacy] = await Promise.all(paths.map((path) => api(path)));
  state.projects = projects.projects; state.environments = environments.environments;
  state.locations = locations.locations; state.drafts = drafts.drafts; state.jobs = jobs.jobs;
  state.legacy = legacy.drafts.filter((d) => d.origin === 'legacy');
  renderProjects(); renderWork();
}
for (const name of ['work', 'projects', 'demo']) $(`tab-${name}`).addEventListener('click', () => { if (!leaving()) return; state.dirtyDraft = false; state.dirtyProject = false; state.folder = null; state.draft = null; renderProjects(); renderWork(); pane(name); });
$('new-work').addEventListener('click', () => { if (!state.projects.length) { pane('projects'); $('new-project-name').focus(); return; } if (!leaving()) return; $('new-work-panel').hidden = false; $('work-project').focus(); });
$('empty-action').addEventListener('click', () => $('new-work').click());
$('close-new-work').addEventListener('click', () => { $('new-work-panel').hidden = true; });
$('create-work').addEventListener('click', () => act(async () => { if (!$('work-project').value) throw error('請選擇已登錄的專案。'); await newWork($('work-project').value); }));
$('refresh').addEventListener('click', () => act(async () => { if (!leaving()) return; state.draft = null; state.dirtyDraft = false; state.dirtyProject = false; state.folder = null; if (state.job) state.job = await api(`/api/jobs/${state.job.id}`); if (state.project) state.project = await api(`/api/projects/${state.project.id}`); await refresh(); notice('已讀取本機紀錄，沒有查詢國網。'); }));
$('create-project-form').addEventListener('submit', (event) => { event.preventDefault(); if (!valid(event.target)) return; act(async () => { if (!leaving()) return; state.folder = null; rememberProject(await api('/api/projects', 'POST', {name: $('new-project-name').value.trim()})); $('new-project-name').value = ''; notice('專案已建立，請匯入程式資料夾或載入多檔範例。'); }); });
$('project-form').addEventListener('input', (event) => { if (!event.target.id.startsWith('metadata-') && event.target.type !== 'file') { state.dirtyProject = true; $('project-save-state').textContent = '有未保存設定'; } });
$('project-form').addEventListener('change', (event) => { if (!event.target.id.startsWith('metadata-') && event.target.type !== 'file') { state.dirtyProject = true; $('project-save-state').textContent = '有未保存設定'; } });
$('project-form').addEventListener('submit', (event) => { event.preventDefault(); if (!valid(event.target)) return; act(async () => { await saveProject(); notice('專案設定已保存，新建工作會使用這份版本。'); }); });
$('project-folder').addEventListener('change', selectFolder);
$('import-folder').addEventListener('click', () => act(importFolder));
$('project-example').addEventListener('click', () => act(async () => { if (state.folder) throw error('請先完成或放棄目前選取的資料夾；範例只供空專案使用。'); const p = await saveProject(); rememberProject(await api(`/api/projects/${p.id}/example`, 'POST', {revision: p.revision})); notice('已載入明標可信多檔範例；正式工作與訓練品質仍待驗收。'); }));
$('reload-project').addEventListener('click', () => act(async () => { if (!leaving()) return; state.folder = null; rememberProject(await api(`/api/projects/${state.project.id}`)); notice('已讀取本機保存的專案。'); }));
$('project-create-work').addEventListener('click', () => act(async () => { if (state.folder) throw error('資料夾尚未匯入，請先確認更新程式快照。'); const p = await saveProject(); await newWork(p.id); }));
$('register-metadata').addEventListener('click', () => act(async () => {
  const kind = $('metadata-kind').value, name = $('metadata-name').value.trim(), path = $('metadata-path').value.trim(); if (!name || !path) throw error('請填寫名稱與既有遠端絕對路徑。');
  const item = await api(kind === 'environment' ? '/api/environments' : '/api/locations', 'POST', kind === 'environment' ? {name, sif_path: path} : {name, kind, path});
  if (kind === 'environment') { state.environments.push(item); options($('project-environment'), state.environments, item.id, '待核對環境'); }
  else { const selected = checked('project-locations'); state.locations.push(item); checkboxes($('project-locations'), [...selected, item.id]); }
  $('metadata-name').value = ''; $('metadata-path').value = ''; state.dirtyProject = true; $('project-save-state').textContent = '有未保存設定'; notice('待核對資訊已登錄，請保存專案設定以使用。');
}));
$('work-form').addEventListener('input', () => { state.dirtyDraft = true; $('work-save-state').textContent = '有未保存設定'; costSummary(); });
$('work-form').addEventListener('change', () => { state.dirtyDraft = true; $('work-save-state').textContent = '有未保存設定'; costSummary(); });
$('resource-preset').addEventListener('change', () => { const preset = presets[$('resource-preset').value]; if (preset) { $('partition').value = preset[0]; $('minutes').value = preset[1]; $('gpus').value = 1; costSummary(); } });
$('partition').addEventListener('change', () => { $('resource-preset').value = 'custom'; });
$('save-work').addEventListener('click', () => { if (valid($('work-form'))) act(async () => { await saveWork(); notice('工作草稿已保存。'); }); });
$('work-form').addEventListener('submit', (event) => { event.preventDefault(); if (!valid(event.target)) return; act(async () => { const draft = await saveWork(), job = await api(`/api/work-drafts/${draft.id}/prepare`, 'POST', {revision: draft.revision}); replace(state.jobs, job); state.drafts = state.drafts.filter((d) => d.id !== draft.id); state.draft = null; state.job = job; renderWork(); notice('工作已準備完成，尚未提交國網。'); }); });
$('leave-work').addEventListener('click', () => { if (!leaving()) return; state.draft = null; state.dirtyDraft = false; renderWork(); });
window.addEventListener('beforeunload', (event) => { if (state.dirtyDraft || state.dirtyProject || state.folder) { event.preventDefault(); event.returnValue = ''; } });
act(async () => { state.bootstrap = await api('/api/bootstrap'); if (state.bootstrap.live_enabled !== false) throw error('服務未確認國網連線停用，已停止載入。'); await refresh(); $('connection-state').textContent = '本機服務已連線 · 國網提交停用'; });
