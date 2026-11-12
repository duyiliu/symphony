
const COLS = ['idle', 'ready', 'running', 'retrying', 'blocked', 'awaiting', 'done'];
const EMPTY = {
  idle: '没有空闲的 Issue',
  ready: '没有等待派发的 Issue',
  running: '没有正在执行的 Issue',
  retrying: '没有等待重试的 Issue',
  blocked: '没有异常状态',
  awaiting: '没有等待合并的 PR',
  done: '没有已完成记录'
};
const MAX_MSGS = 400;

let CUR = null, CARD = null, RUNS = [], AGENT = null, TROUBLE = null, LOG = null;
let SHOW_EMPTY = localStorage.getItem('sb-show-empty') !== '0';
let PENDING_RUN = new URLSearchParams(location.search).get('run');
let BUSY = false, TOAST_TIMER = null, WRITABLE = false;
const PENDING_OPERATIONS = new Map();

function trackOperation(issue, op, phase = 'confirmed') {
  const card = boardEl.querySelector(".bcard[data-issue='" + issue + "']");
  PENDING_OPERATIONS.set(issue, {op: op, phase: phase, at: Date.now(),
    wasRunning: card && card.getAttribute('data-state') === 'running'});
  renderPendingCards();
  renderActions();
}

function renderPendingCards() {
  boardEl.querySelectorAll('.pending-sync').forEach(el => el.remove());
  boardEl.querySelectorAll('.bcard').forEach(card => {
    if (card.getAttribute('data-closed') === '1') { return; }
    const message = operationNotice(card.getAttribute('data-issue'));
    if (message) {
      const pending = PENDING_OPERATIONS.get(card.getAttribute('data-issue'));
      const view = operationView(pending, {
        state: card.getAttribute('data-state'),
        mergePending: card.getAttribute('data-mg') === '1'
      });
      if (view.state) {
        const before = card.getAttribute('data-state');
        card.classList.remove(before);
        card.classList.add(view.state);
        card.setAttribute('data-state', view.state);
        const lane = document.getElementById('col-' + view.state);
        if (lane) { lane.appendChild(card); }
      }
      card.setAttribute('data-state-text', view.text);
      const stat = card.querySelector('.bstat');
      if (stat) {
        stat.textContent = view.text;
        if (view.state) { stat.className = 'bstat tone-' + view.state; }
      }
      if (pending.phase === 'confirmed') {
        if (pending.op === 'pause' || pending.op === 'resume') {
          card.setAttribute('data-paused', pending.op === 'pause' ? '1' : '0');
          card.setAttribute('data-agent', pending.op === 'resume' ? '1' : '0');
        }
        if (pending.op === 'approve' || pending.op === 'revoke') {
          card.setAttribute('data-approved', pending.op === 'approve' ? '1' : '0');
        }
        if (pending.op === 'rework') {
          card.setAttribute('data-approved', '0');
          card.setAttribute('data-agent', '1');
          card.setAttribute('data-paused', '0');
        }
      }
      const notice = document.createElement('div');
      notice.className = 'pending-sync an';
      notice.textContent = message;
      card.appendChild(notice);
    }
  });
  applyFilter();
  if (ISSUES_READY) {
    const summary = [];
    for (const key of COLS) {
      const col = document.querySelector(".col[data-col='" + key + "']");
      if (!col) { continue; }
      const count = col.querySelectorAll('.bcard').length;
      if (count) { summary.push(count + ' ' + col.querySelector('.clabel').textContent); }
    }
    const sum = document.getElementById('sum');
    if (sum) { sum.textContent = summary.join(' · ') || '当前没有任务'; }
  }
  if (CUR) { refreshCard(); }
}

function operationView(pending, current) {
  if (pending.phase === 'uncertain') { return {text: '操作结果待确认'}; }
  if (pending.phase === 'partial') { return {text: '操作未完整完成 · 正在核对'}; }
  if (pending.phase === 'submitting') { return {text: '正在提交操作'}; }
  switch (pending.op) {
    case 'approve': return {state: 'awaiting', text: '已批准 · 等待核验'};
    case 'revoke': return {state: 'awaiting', text: '批准已撤销 · 等待核验'};
    case 'pause': return {state: 'blocked', text: pending.wasRunning
      ? '已转人工 · 正在停止执行' : '等待人工处理'};
    case 'resume': return {state: current.mergePending ? 'awaiting' : 'ready', text: '已恢复 · 等待调度器核对'};
    case 'start': return {state: 'ready', text: '已加入队列 · 等待调度器领取'};
    case 'rework': return {state: 'ready', text: '意见已提交 · 等待 Agent 返工'};
    case 'stop': return {state: pending.wasRunning ? 'blocked' : 'idle', text: pending.wasRunning
      ? '暂停已提交 · 正在停止执行' : '已暂停 · 等待调度器核对'};
    default: return {text: '标签已更新 · 等待同步'};
  }
}

function operationNotice(issue) {
  const pending = PENDING_OPERATIONS.get(issue);
  if (!pending) { return ''; }
  const delayed = Date.now() - pending.at > 60000;
  return (pending.phase === 'uncertain' ? '请求结果不确定，正在重新读取 GitHub；请勿重复提交'
    : pending.phase === 'partial' ? '操作未完整完成，正在重新读取 GitHub 标签'
    : pending.op === 'start' ? '开始执行已提交，等待调度器领取；不代表 AI 已启动'
    : pending.op === 'rework' ? '意见已发布到 PR，旧批准已撤销；等待调度器领取返工任务'
    : pending.op === 'resume' ? '恢复已提交，等待调度器核对；不代表 AI 已启动'
    : pending.op === 'pause' ? '暂停标签已更新，运行中的任务仍需等待停止确认'
    : pending.op === 'approve' ? '批准已提交；宿主仍需核验当前 SHA、检查和有效审批'
    : pending.op === 'revoke' ? '批准已撤销；若合并已发生则不能撤回'
    : pending.op === 'stop' ? '暂停已提交；调度器确认停止前，任务可能仍在运行'
    : '标签已更新，等待状态同步')
    + (delayed ? '；同步延迟，请检查调度器状态。' : '。');
}

function confirmOperations(data) {
  for (const [issue, pending] of PENDING_OPERATIONS) {
    const facts = (data.facts || {})[issue];
    if (facts && facts.closed) { PENDING_OPERATIONS.delete(issue); continue; }
    if (!facts || data.github_observed_after * 1000 < pending.at) { continue; }
    const labels = facts.labels || [];
    if (pending.phase === 'submitting') { continue; }
    if (pending.phase === 'partial') {
      PENDING_OPERATIONS.delete(issue);
      continue;
    }
    const labelMatches = ['start', 'resume', 'rework'].includes(pending.op) ? labels.includes('agent') && !labels.includes('needs-human')
      && (pending.op !== 'rework' || !labels.includes('merge-approved'))
      : pending.op === 'pause' ? labels.includes('needs-human') && !labels.includes('agent')
      : pending.op === 'approve' ? labels.includes('merge-approved')
      : pending.op === 'revoke' ? !labels.includes('merge-approved')
      : pending.op === 'stop' ? !labels.includes('agent') : true;
    if (!labelMatches) { continue; }
    const runtime = (data.runtime || {})[issue];
    if (pending.op === 'rework' && (data.merge_waiting || []).includes(issue)) { continue; }
    const expected = ['start', 'resume', 'rework'].includes(pending.op)
      ? runtime !== 'blocked' && (pending.op !== 'rework' || runtime !== 'awaiting')
      : pending.op === 'pause' || pending.op === 'stop' ? runtime !== 'running' && runtime !== 'retrying' : true;
    if (expected && (['approve', 'revoke', 'labels'].includes(pending.op)
        || data.state_observed_after * 1000 >= pending.at)) {
      PENDING_OPERATIONS.delete(issue);
    }
  }
}

async function postGithubOperation(url, payload, issue, op) {
  let res;
  try {
    res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)});
  } catch (err) {
    trackOperation(issue, op, 'uncertain');
    throw new Error('连接中断，操作结果待确认；请勿重复提交');
  }
  let data;
  try { data = await res.json(); } catch (err) {
    trackOperation(issue, op, 'uncertain');
    throw new Error('无法确认服务端结果，正在核对 GitHub');
  }
  if (!res.ok) {
    if (data.outcome === 'partial' || data.outcome === 'uncertain' || (res.status >= 500 && !data.outcome)) {
      trackOperation(issue, op, data.outcome === 'partial' ? 'partial' : 'uncertain');
    }
    throw new Error(data.error || ('HTTP ' + res.status));
  }
  trackOperation(issue, op);
  return data;
}

// Legacy board credentials are no longer used. Strip old token links without
// storing their credentials; all authorization now happens through GitHub labels.
(function () {
  localStorage.removeItem('sb-token');
  const params = new URLSearchParams(location.search);
  if (!params.has('token')) { return; }
  params.delete('token');
  const query = params.toString();
  history.replaceState(null, '', location.pathname + (query ? '?' + query : ''));
})();

const DIALOG_ICONS = {
  info: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'><circle cx='12' cy='12' r='9'/><path d='M12 10.8v5.4'/><circle cx='12' cy='7.6' r='1.1' fill='currentColor' stroke='none'/></svg>",
  warn: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M12 3.6 21 20H3z'/><path d='M12 9.4v4.4'/><circle cx='12' cy='16.7' r='1.05' fill='currentColor' stroke='none'/></svg>",
  danger: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M8.3 3.2h7.4l5.1 5.1v7.4l-5.1 5.1H8.3l-5.1-5.1V8.3z'/><path d='M12 7.8v5.1'/><circle cx='12' cy='16.2' r='1.05' fill='currentColor' stroke='none'/></svg>",
  ok: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='9'/><path d='m8 12.2 2.7 2.7L16.2 9.4'/></svg>"
};
let DIALOG = null;

function openDialog(opts) {
  const root = document.getElementById('modal');
  if (!root) { return Promise.resolve(null); }
  if (DIALOG) { DIALOG.finish(null); }
  opts = opts || {};
  return new Promise(function (resolve) {
    const tone = opts.tone || 'info';
    const titleEl = document.getElementById('dlg-title');
    const bodyEl = document.getElementById('dlg-body');
    const iconEl = document.getElementById('dlg-icon');
    const fieldEl = document.getElementById('dlg-field');
    const inputEl = document.getElementById('dlg-input');
    const countEl = document.getElementById('dlg-count');
    const okEl = document.getElementById('dlg-ok');
    const cancelEl = document.getElementById('dlg-cancel');
    const prevFocus = document.activeElement;
    const hasInput = !!opts.input;
    let done = false;

    titleEl.textContent = opts.title || '确认操作';
    bodyEl.textContent = opts.body || '';
    bodyEl.hidden = !opts.body;
    iconEl.className = 'dlg-icon ' + tone;
    iconEl.innerHTML = DIALOG_ICONS[tone] || DIALOG_ICONS.info;
    okEl.className = 'dlg-btn primary ' + tone;
    okEl.textContent = opts.confirmText || '确认';
    cancelEl.textContent = opts.cancelText || '取消';
    cancelEl.hidden = opts.cancel === false;
    fieldEl.hidden = !hasInput;
    if (hasInput) {
      inputEl.value = opts.input.value || '';
      inputEl.placeholder = opts.input.placeholder || '';
      inputEl.maxLength = opts.input.maxlength || 4000;
      inputEl.oninput = function () {
        countEl.textContent = inputEl.value.length + ' / ' + inputEl.maxLength;
        countEl.classList.toggle('limit', inputEl.value.length >= inputEl.maxLength);
      };
      inputEl.oninput();
    } else {
      inputEl.value = '';
      inputEl.oninput = null;
    }

    function finish(value) {
      if (done) { return; }
      done = true;
      DIALOG = null;
      root.classList.remove('open');
      root.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('dialog-open');
      document.removeEventListener('keydown', onKey, true);
      root.removeEventListener('click', onBackdrop);
      okEl.removeEventListener('click', onOk);
      cancelEl.removeEventListener('click', onCancel);
      inputEl.oninput = null;
      if (prevFocus && typeof prevFocus.focus === 'function') {
        try { prevFocus.focus(); } catch (err) { }
      }
      resolve(value);
    }
    function onOk() { finish(hasInput ? inputEl.value : true); }
    function onCancel() { finish(null); }
    function onBackdrop(e) { if (e.target === root) { finish(null); } }
    function onKey(e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        finish(null);
        return;
      }
      if (e.key === 'Enter') {
        if (hasInput) {
          if (e.ctrlKey || e.metaKey) { e.preventDefault(); onOk(); }
        } else if (!e.target || e.target.tagName !== 'BUTTON') {
          e.preventDefault();
          onOk();
        }
        return;
      }
      if (e.key === 'Tab') {
        const items = (hasInput ? [inputEl] : []).concat(cancelEl.hidden ? [] : [cancelEl], [okEl]);
        const first = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }

    okEl.addEventListener('click', onOk);
    cancelEl.addEventListener('click', onCancel);
    root.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    root.classList.add('open');
    root.setAttribute('aria-hidden', 'false');
    document.body.classList.add('dialog-open');
    DIALOG = { finish: finish };
    setTimeout(function () {
      if (done) { return; }
      const focusTarget = hasInput ? inputEl : (tone === 'danger' && !cancelEl.hidden ? cancelEl : okEl);
      focusTarget.focus();
    }, 30);
  });
}

function toast(message, err) {
  const el = document.getElementById('toast');
  if (!el) { return; }
  const icon = el.querySelector('.ticon');
  const text = el.querySelector('.tmsg');
  if (icon) { icon.innerHTML = err ? DIALOG_ICONS.danger : DIALOG_ICONS.ok; }
  if (text) { text.textContent = message; } else { el.textContent = message; }
  el.className = 'on' + (err ? ' err' : '');
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(function () { el.className = ''; }, 4200);
}

const boardEl = document.getElementById('board');
const shadeEl = document.getElementById('shade');
const drawerEl = document.getElementById('drawer');
const dheadEl = document.getElementById('dhead');
const dbodyEl = document.getElementById('dbody');
const dgripEl = document.getElementById('dgrip');
WRITABLE = boardEl.getAttribute('data-writable') === '1';

const DRAWER_W_KEY = 'sym.drawer.w';
const drawerWClamp = function (w) {
  return Math.min(Math.max(w, 420), Math.floor(window.innerWidth * 0.94));
};
(function () {
  const saved = parseInt(localStorage.getItem(DRAWER_W_KEY) || '', 10);
  if (saved >= 420) { drawerEl.style.width = drawerWClamp(saved) + 'px'; }
  window.addEventListener('resize', function () {
    if (drawerEl.style.width) {
      drawerEl.style.width = drawerWClamp(parseInt(drawerEl.style.width, 10)) + 'px';
    }
  });
})();
if (dgripEl) {
  dgripEl.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) { return; }
    e.preventDefault();
    dgripEl.setPointerCapture(e.pointerId);
    document.body.classList.add('resizing');
    const move = function (ev) {
      drawerEl.style.width = drawerWClamp(window.innerWidth - ev.clientX) + 'px';
    };
    const up = function () {
      dgripEl.removeEventListener('pointermove', move);
      dgripEl.removeEventListener('pointerup', up);
      dgripEl.removeEventListener('pointercancel', up);
      document.body.classList.remove('resizing');
      localStorage.setItem(DRAWER_W_KEY, String(Math.round(drawerEl.getBoundingClientRect().width)));
    };
    dgripEl.addEventListener('pointermove', move);
    dgripEl.addEventListener('pointerup', up);
    dgripEl.addEventListener('pointercancel', up);
  });
}

const ICON = {
  running: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6' opacity='.35'/><circle class='spin' cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-dasharray='26 35.2' transform='rotate(-90 8 8)' style='transform-origin:8px 8px'/></svg>",
  blocked: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6'/><path d='M8 4.9v3.6' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'/><circle cx='8' cy='10.9' r='.95' fill='currentColor'/></svg>",
  retrying: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-dasharray='9.5 7.5'/></svg>",
  ready: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6'/></svg>",
  awaiting: "<svg viewBox='0 0 16 16'><circle cx='4.6' cy='4.2' r='1.6' fill='none' stroke='currentColor' stroke-width='1.6'/><circle cx='4.6' cy='11.8' r='1.6' fill='none' stroke='currentColor' stroke-width='1.6'/><circle cx='11.4' cy='8' r='1.6' fill='none' stroke='currentColor' stroke-width='1.6'/><path d='M4.6 5.8v4.4M6.2 8h3.6' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'/></svg>",
  idle: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.4' opacity='.55'/></svg>",
  done: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6'/><path d='M5.6 8.2l1.7 1.7 3.2-3.5' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/></svg>",
  ext: "<svg viewBox='0 0 16 16'><path fill='none' stroke='currentColor' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round' d='M6.5 3.5h-3v9h9v-3M9.5 3.5h3v3M12 4 8 8'/></svg>",
  file: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><path d='M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'/><path d='M14 3v5h5'/></svg>"
};

function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function dur(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) { return s + ' 秒'; }
  if (s < 3600) { return Math.floor(s / 60) + ' 分'; }
  return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
}
function ago(iso) {
  const t = Date.parse(iso || '');
  return isNaN(t) ? '' : dur(Date.now() - t);
}
function fmtKB(n) {
  if (!n) { return '0B'; }
  if (n < 1024) { return n + 'B'; }
  if (n < 1048576) { return (n / 1024).toFixed(1) + 'K'; }
  return (n / 1048576).toFixed(1) + 'M';
}
function tickDurations() {
  document.querySelectorAll('[data-since]').forEach(function (el) {
    const t = Date.parse(el.getAttribute('data-since') || '');
    if (!isNaN(t)) { el.textContent = '已运行 ' + dur(Date.now() - t); }
  });
}
setInterval(tickDurations, 1000);

function markSel() {
  document.querySelectorAll('.bcard[data-issue]').forEach(function (c) {
    c.classList.toggle('sel', c.getAttribute('data-issue') === CUR);
  });
}

boardEl.addEventListener('click', function (e) {
  const card = e.target.closest('.bcard[data-issue]');
  if (!card || e.target.closest('a')) { return; }
  openIssue(card.getAttribute('data-issue'));
});

document.addEventListener('keydown', function (e) {
  const menu = document.querySelector('.action-more[open]');
  if (e.key === 'Escape' && menu) {
    menu.open = false;
    menu.querySelector('summary').focus();
    e.preventDefault();
    return;
  }
  if (e.key === 'Escape' && CUR) { closeDrawer(); }
});

document.addEventListener('click', function (e) {
  document.querySelectorAll('.action-more[open]').forEach(menu => {
    if (!menu.contains(e.target)) { menu.open = false; }
  });
});

function openIssue(name) {
  CUR = name;
  RUNS = []; AGENT = null; LOG = null; TROUBLE = null;
  markSel();
  syncUrl(name);
  const card = (VIEW === 'active' ? boardEl : document.getElementById('completed-list')).querySelector(".bcard[data-issue='" + name + "']");
  CARD = card ? {
    name: name,
    title: card.getAttribute('data-title') || name,
    url: card.getAttribute('data-url') || '',
    state: card.getAttribute('data-state') || 'idle',
    text: card.getAttribute('data-state-text') || '',
    agent: card.getAttribute('data-agent') === '1',
    mergePending: card.getAttribute('data-mg') === '1',
    approved: card.getAttribute('data-approved') === '1',
    paused: card.getAttribute('data-paused') === '1',
    pauseReason: card.getAttribute('data-pause-reason') || '',
    closed: card.getAttribute('data-closed') === '1',
    completedAt: card.getAttribute('data-completed-at') || '',
    description: card.getAttribute('data-description') || '',
    pr: card.getAttribute('data-pr') || '',
    prNum: card.getAttribute('data-pr-num') || ''
  } : { name: name, title: name, url: '', state: 'idle', text: '', agent: false, paused: false, pauseReason: '', pr: '', prNum: '' };
  dheadEl.innerHTML = "<button class='dclose' id='dclose' title='关闭'>&#8249;</button>"
    + "<div class='dinfo'><div class='dtop'>"
    + "<a class='did' href='" + escHtml(CARD.url) + "' target='_blank' rel='noopener noreferrer'>" + escHtml(CARD.name) + "</a>"
    + "<span class='dstat tone-" + escHtml(CARD.state) + "'>" + ICON[CARD.state] + escHtml(CARD.text) + "</span>"
    + "</div><div class='dtitle'>" + escHtml(CARD.title) + "</div></div>"
    + "<a class='dext' href='" + escHtml(CARD.url) + "' target='_blank' rel='noopener noreferrer' title='在 GitHub 打开'>" + ICON.ext + "</a>";
  dbodyEl.innerHTML = "<div class='dnote'>加载中…</div>";
  shadeEl.classList.add('open');
  drawerEl.classList.add('open');
  loadRuns(true);
}

function closeDrawer() {
  CUR = null; CARD = null; RUNS = []; AGENT = null; TROUBLE = null; LOG = null;
  markSel();
  syncUrl(null);
  shadeEl.classList.remove('open');
  drawerEl.classList.remove('open');
}

function syncUrl(name) {
  const url = new URL(location.href);
  if (name) { url.searchParams.set('issue', name); }
  else { url.searchParams.delete('issue'); }
  history.replaceState(null, '', url);
}

function troubleHtml() {
  if (!TROUBLE || !TROUBLE.short) { return ''; }
  const due = TROUBLE.due_at ? new Date(TROUBLE.due_at) : null;
  const dueText = due && !isNaN(due.getTime())
    ? ' · 下次 ' + due.toLocaleTimeString('zh-CN', { hour12: false })
    : '';
  return "<div class='nbe' title='" + escHtml(TROUBLE.error || TROUBLE.short) + "'>"
    + escHtml(TROUBLE.short)
    + (TROUBLE.attempt ? ' · 第 ' + TROUBLE.attempt + ' 次' : '')
    + dueText + "</div>";
}

function refreshCard() {
  if (!CUR) { return; }
  const card = (VIEW === 'active' ? boardEl : document.getElementById('completed-list')).querySelector(".bcard[data-issue='" + CUR + "']");
  if (!card) { return; }
  CARD.closed = card.getAttribute('data-closed') === '1';
  CARD.state = card.getAttribute('data-state') || CARD.state;
  CARD.text = card.getAttribute('data-state-text') || CARD.text;
  CARD.title = card.getAttribute('data-title') || CARD.title;
  CARD.agent = card.getAttribute('data-agent') === '1';
  CARD.mergePending = card.getAttribute('data-mg') === '1';
  CARD.approved = card.getAttribute('data-approved') === '1';
  CARD.paused = card.getAttribute('data-paused') === '1';
  CARD.pauseReason = card.getAttribute('data-pause-reason') || '';
  CARD.pr = card.getAttribute('data-pr') || '';
  CARD.prNum = card.getAttribute('data-pr-num') || '';
}

function moreActionsHtml() {
  return "<details class='action-more'" + (BUSY ? ' inert' : '') + "><summary>更多操作</summary>"
    + "<div class='action-menu'><button class='abtn warn' data-intervene='pause'"
    + (BUSY ? ' disabled' : '') + ">转人工处理</button>"
    + "<p class='an'>暂停自动处理，并标记需要人工介入。</p></div></details>";
}

function actionsHtml() {
  if (!CARD) { return ''; }
  if (PENDING_OPERATIONS.has(CUR)) {
    return "<section class='actions' id='actions'><p class='an'>" + escHtml(operationNotice(CUR))
      + "</p><button class='abtn' data-sync-check>重新核对</button>"
      + (Date.now() - PENDING_OPERATIONS.get(CUR).at > 60000
        ? "<button class='abtn warn' data-sync-dismiss>已人工核对，返回操作</button>" : '') + "</section>";
  }
  let inner;
  if (CARD.closed) {
    return "<section class='actions' id='actions'><p class='an'>Issue 已完成，可在此查看详情和本地日志。无需再次派发或批准。</p></section>";
  }
  if (!WRITABLE) {
    inner = "<p class='an'>写操作未启用：服务端未配置 GitHub 凭据（SYMPHONY_WORKFLOW / SYMPHONY_ENV）。</p>";
  } else if (CARD.agent && CARD.paused) {
    inner = "<p class='an'>agent 与 needs-human 同时存在，Symphony 已停止执行并记录异常；摘掉其中一个标签可恢复对应流程。</p>"
      + "<div class='abtns'>"
      + "<button class='abtn warn' data-act='remove' data-label='agent'" + (BUSY ? ' disabled' : '') + ">摘掉 agent</button>"
      + "<button class='abtn warn' data-act='remove' data-label='needs-human'" + (BUSY ? ' disabled' : '') + ">摘掉 needs-human</button>"
      + "</div>";
  } else if (CARD.paused) {
    inner = "<p class='an'>已暂停等待人工。" + (CARD.pauseReason ? '<br>原因：' + escHtml(CARD.pauseReason) : '<br>原因见 issue 内的暂停评论。')
      + "</p>"
      + "<div class='abtns'>"
      + "<button class='abtn ok' data-intervene='resume'" + (BUSY ? ' disabled' : '') + ">"
      + (BUSY ? '正在提交…' : '恢复执行') + "</button></div>";
  } else if (CARD.agent && CARD.mergePending) {
    inner = CARD.approved
      ? "<p class='an'>已挂 merge-approved；宿主会在下一轮合并评估时放行冻结的交付 SHA。</p>"
        + "<div class='action-toolbar'><div class='abtns'>"
        + "<button class='abtn warn' data-act='remove' data-label='merge-approved'" + (BUSY ? ' disabled' : '') + ">"
        + (BUSY ? '正在提交…' : '撤销批准') + "</button>"
      : "<p class='an'>交付已发布，宿主会定期检查合并条件。请先查看 PR，再决定是否批准。</p>"
        + "<div class='action-toolbar'><div class='abtns'>"
        + "<button class='abtn ok' data-act='add' data-label='merge-approved'" + (BUSY ? ' disabled' : '') + ">"
        + (BUSY ? '正在提交…' : '批准合并') + "</button>"
        + (CARD.pr ? "<a class='abtn' href='" + escHtml(CARD.pr) + "' target='_blank' rel='noopener noreferrer'>查看 PR</a>" : '');
    inner += (CARD.prNum ? "<button class='abtn' data-rework" + (BUSY ? ' disabled' : '') + ">继续优化</button>" : '')
      + "</div>" + moreActionsHtml() + "</div>";
  } else if (CARD.agent) {
    inner = "<p class='an'>暂停后不再自动执行；运行中的任务需要等待调度器确认停止。</p>"
      + "<div class='action-toolbar'><div class='abtns'>"
      + "<button class='abtn warn' data-act='remove' data-label='agent'"
      + (BUSY ? ' disabled' : '') + ">" + (BUSY ? '正在提交…' : '暂停执行') + "</button></div>"
      + moreActionsHtml() + "</div>";
  } else {
    inner = "<p class='an'>给此 Issue 加上 agent 标签，调度器会按 Issue 内容领取任务。</p>"
      + "<div class='abtns'>"
      + "<button class='abtn ok' data-act='add' data-label='agent'" + (BUSY ? ' disabled' : '') + ">"
      + (BUSY ? '正在提交…' : '开始执行') + "</button></div>";
  }
  return "<section class='actions' id='actions'><p class='at'>任务操作</p>" + inner + "</section>";
}

function renderActions() {
  const el = document.getElementById('actions');
  if (!el) { return; }
  el.outerHTML = actionsHtml();
}

async function reworkAction() {
  if (!CUR || BUSY || !CARD || !CARD.prNum) { return; }
  const issue = CUR;
  const pr = CARD.prNum;
  const feedback = await openDialog({
    title: '继续优化 ' + issue,
    body: '意见将发布到 PR #' + pr + '，撤销已有批准，并重新排队。Agent 会继续修改这个 PR；提交意见本身不会触发 CI，后续推送新提交时才可能触发。',
    input: {placeholder: '请填写具体修改意见（必填）', maxlength: 1000},
    confirmText: '提交意见并重新排队'
  });
  if (feedback === null) { return; }
  const comment = String(feedback).trim();
  if (!comment) { toast('请填写修改意见', true); return; }
  BUSY = true;
  renderActions();
  try {
    const data = await postGithubOperation('/api/rework', {issue, pr, comment}, issue, 'rework');
    toast(data.message);
    await refreshIssues(true);
  } catch (err) {
    toast(err.message || '提交失败', true);
    await refreshIssues(true);
  } finally {
    BUSY = false;
    renderActions();
  }
}

async function interveneAction(op) {
  if (!CUR || BUSY) { return; }
  let comment = '';
  if (op === 'pause') {
    const reason = await openDialog({
      tone: 'warn',
      title: '转人工处理 ' + CUR,
      body: '暂停后不会自动合并，恢复执行后才会继续处理。可填写原因（可选），会作为 issue 评论发布。',
      input: { placeholder: '例如：需要人工确认合并范围 / 先暂停观察', maxlength: 4000 },
      confirmText: '转人工处理'
    });
    if (reason === null) { return; }
    comment = String(reason).trim();
  } else if (!(await openDialog({
    tone: 'ok',
    title: '恢复执行 ' + CUR,
    body: '会移除 needs-human 并重新加 agent；合并前宿主会重新核验当前 SHA 的检查与有效审批，重贴 agent 本身不是合并授权。',
    confirmText: '恢复执行'
  }))) { return; }
  const issue = CUR;
  BUSY = true;
  renderActions();
  try {
    const data = await postGithubOperation('/api/intervene', {issue, op, comment}, issue, op);
    toast(data.message || '已提交');
    await refreshIssues(true);
    refreshCard();
    await loadRuns(true);
  } catch (err) {
    toast(err.message || '操作失败', true);
    await refreshIssues(true);
  } finally {
    BUSY = false;
    renderActions();
  }
}

async function labelAction(action, label) {
  if (!CUR || BUSY) { return; }
  label = label || 'agent';
  const comment = '';
  const running = CARD.state === 'running';
  let dialog;
  if (action === 'add' && label === 'agent') {
    dialog = null;
  } else if (action === 'add' && label === 'merge-approved') {
    dialog = { tone: 'ok', title: '批准合并 ' + CUR, body: '挂 merge-approved 标签？宿主会在下一轮合并评估放行当前冻结的交付 SHA——此操作视同人工批准，请先确认 PR 内容。', confirmText: '批准合并' };
  } else if (action === 'add') {
    dialog = { tone: 'warn', title: '转人工处理 ' + CUR, body: '加 needs-human 并摘掉 agent 标签？调度器不会再派发，直到人工恢复。', confirmText: '转人工处理' };
  } else if (action === 'remove' && label === 'merge-approved') {
    dialog = { tone: 'warn', title: '撤销批准 ' + CUR, body: '摘掉 merge-approved 标签？宿主下轮评估会把该交付重新判为未批准，不再自动合并（若已放行则无法撤回）。', confirmText: '撤销批准' };
  } else if (label === 'agent') {
    dialog = running
      ? { tone: 'warn', title: '暂停执行 ' + CUR, body: '暂停此任务？会移除 agent 标签，运行中的任务需要等待调度器确认停止。', confirmText: '暂停执行' }
      : { tone: 'warn', title: '暂停执行 ' + CUR, body: '暂停后，此任务不再被自动领取。之后可点击开始执行重新加入队列。', confirmText: '暂停执行' };
  } else {
    dialog = { tone: 'warn', title: '摘掉 needs-human ' + CUR, body: '摘掉 needs-human 标签？如需继续执行，请再重新加 agent。', confirmText: '摘掉标签' };
  }
  if (dialog && !(await openDialog(dialog))) { return; }
  const issue = CUR;
  const operation = label === 'merge-approved' ? (action === 'add' ? 'approve' : 'revoke')
    : label === 'agent' ? (action === 'add' ? 'start' : 'stop')
    : action === 'add' ? 'pause' : 'labels';
  BUSY = true;
  renderActions();
  try {
    const data = await postGithubOperation('/api/label', {issue, action, label, comment}, issue, operation);
    toast(data.message || '已提交');
    await refreshIssues(true);
    refreshCard();
    await loadRuns(true);
  } catch (err) {
    toast(err.message || '操作失败', true);
    await refreshIssues(true);
  } finally {
    BUSY = false;
    renderActions();
  }
}

function noteBoxHtml() {
  const st = CARD.state;
  let title = CARD.text, body = '';
  if (CARD.closed) {
    title = '已完成';
    body = '完成于 ' + escHtml(CARD.completedAt) + '（北京时间）。GitHub Issue 已关闭，不代表已独立核实 PR 合并。'
      + (CARD.description ? "<details class='completed-description'><summary>Issue 内容</summary><pre>" + escHtml(CARD.description) + '</pre></details>' : '');
  } else if (st === 'running' && AGENT) {
    title = '正在执行';
    body = '▸ ' + escHtml(AGENT.message || AGENT.event || 'agent 运行中')
      + (AGENT.event_at ? ' · ' + ago(AGENT.event_at) + '前' : '')
      + (AGENT.started_at ? " · <span data-since='" + escHtml(AGENT.started_at) + "'>已运行</span>" : '')
      + (AGENT.tokens ? ' · tokens ' + AGENT.tokens : '')
      + (RUNS.length ? '' : '<br>日志尚未生成，pi 会话开始后自动出现。');
  } else if (st === 'running') {
    title = '正在执行';
    body = 'agent 运行中，等待最新事件。';
  } else if (st === 'blocked') {
    title = '已阻塞';
    if (CARD.paused) {
      body = '已暂停等待人工处理（needs-human）。'
        + (CARD.pauseReason ? '<br>原因：' + escHtml(CARD.pauseReason) : '')
        + '<br>在 GitHub 或本页处理完后点“恢复执行”；重贴 agent 不是合并授权，合并前会重新核验当前 SHA 与审批。';
    } else {
      body = '运行失败或需要人工处理；修复后可重新加上 agent 标签让调度器派发。' + troubleHtml();
    }
  } else if (st === 'retrying') {
    title = '等待重试';
    body = '调度器会在退避窗口结束后重新尝试。' + troubleHtml();
  } else if (st === 'awaiting') {
    title = '等待合并';
    body = CARD.mergePending
      ? '交付已发布，宿主每 30 秒自评合并门禁。低风险 CI 绿即自动合；命中风险路径需人工批准——' + (CARD.approved ? '已批准，等待宿主放行。' : '可先点下方"批准合并"挂 merge-approved。')
      : 'PR 已开；agent 交付后宿主会自动校验检查与风险规则并完成合并，高风险或需人工审批时会等待 Review / 暂停转人工。';
  } else if (st === 'ready') {
    title = '等待派发';
    body = 'Issue 带有 agent 标签，调度器会在下个轮询周期领取。';
  } else {
    title = '空闲';
    body = RUNS.length
      ? '最近一次运行 ' + escHtml(RUNS[0].started || '') + '。'
      : '还没有运行记录；加上 agent 标签后调度器会开始执行。';
  }
  return "<section class='note-box " + escHtml(st) + "' id='nbox'><div class='nbt'>"
    + ICON[st] + escHtml(title) + "</div><div class='nbb'>" + body + "</div></section>";
}

function metaGridHtml() {
  const latest = RUNS[0] || null;
  const items = [
    ['Issue', escHtml(CARD.name)],
    ['状态', escHtml(CARD.text)],
    ['已记录会话', String(RUNS.length)],
    ['最近开始', escHtml(latest ? latest.started : '—')],
    ['日志最后更新', escHtml(latest ? (latest.ended || '—') : '—')],
    ['日志大小', escHtml(latest ? latest.size : '—')]
  ];
  if (AGENT && AGENT.tokens) { items.push(['tokens', escHtml(String(AGENT.tokens))]); }
  if (AGENT && AGENT.model) { items.push(['模型', escHtml(String(AGENT.model))]); }
  if (AGENT && AGENT.thinking) { items.push(['思考级别', escHtml(String(AGENT.thinking))]); }
  if (CARD.pr) {
    items.push(['PR', "<a class='mpr' href='" + escHtml(CARD.pr) + "' target='_blank' rel='noopener noreferrer'>#"
      + escHtml(CARD.prNum) + "</a>"]);
  }
  return "<section class='mgrid' id='mgrid'>" + items.map(function (it) {
    return "<div class='mi'><span>" + it[0] + "</span><b>" + it[1] + "</b></div>";
  }).join('') + "</section>";
}

function updateNoteBox() {
  const nb = document.getElementById('nbox');
  if (nb) { nb.outerHTML = noteBoxHtml(); }
  const mg = document.getElementById('mgrid');
  if (mg) { mg.outerHTML = metaGridHtml(); }
  renderActions();
  tickDurations();
}

function runHtml(r, n) {
  const state = r.active ? 'running' : 'idle';
  const text = r.active ? '运行中' : '已结束';
  const sh = escHtml(r.short);
  return "<section class='run' data-short='" + sh + "'>"
    + "<div class='run-head'><span class='run-idx'>会话 " + n + "</span>"
    + "<span class='run-sha'>" + sh + "</span>"
    + "<span class='run-stat tone-" + state + "'>" + ICON[state] + text + "</span>"
    + "<span class='run-time'>开始 " + escHtml(r.started) + " · " + escHtml(r.active ? '运行中' : ('日志更新 ' + (r.ended || '—'))) + "</span></div>"
    + "<div class='run-meta'><span>" + escHtml(r.size) + "</span><span>" + r.messages + " 条消息</span>"
    + (r.live ? "<span>live</span>" : "")
    + "<span class='grow'></span>"
    + "<button class='logbtn' data-log='" + sh + "' aria-pressed='false'>" + ICON.file + "运行日志</button></div>"
    + "<div class='log' id='log-" + sh + "' hidden>"
    + "<div class='log-tools'><span class='log-meta' id='lm-" + sh + "'></span><span class='grow'></span>"
    + "<button class='fbtn' id='fb-" + sh + "' data-follow='" + sh + "' aria-pressed='true'>跟随中</button></div>"
    + "<div class='log-body' id='lb-" + sh + "'></div></div></section>";
}

function renderRuns() {
  dbodyEl.innerHTML = noteBoxHtml() + actionsHtml() + metaGridHtml()
    + "<p class='dnote'>以下为已记录的 Agent 会话；领取次数可能更多，历史派发记录不完整。结束时间以日志最后更新时间近似。</p>"
    + "<div id='runs'>" + (RUNS.length
      ? RUNS.map(function (r, i) { return runHtml(r, RUNS.length - i); }).join('')
      : "<div class='dnote'>该 issue 还没有运行记录。</div>") + "</div>";
  tickDurations();
  if (LOG) { restoreLog(); }
}

function restoreLog() {
  const sec = document.getElementById('log-' + LOG.short);
  if (!sec) { LOG = null; return; }
  sec.hidden = false;
  const btn = document.querySelector(".logbtn[data-log='" + LOG.short + "']");
  if (btn) { btn.setAttribute('aria-pressed', 'true'); }
  LOG.offset = -1; LOG.busy = false; LOG.dead = false; LOG.live = true;
  const lb = document.getElementById('lb-' + LOG.short);
  if (lb) { lb.innerHTML = ''; }
  updateFollowBtn();
  pollLog();
}

function updateRunStates() {
  RUNS.forEach(function (r) {
    const sec = document.querySelector(".run[data-short='" + r.short + "']");
    if (!sec) { return; }
    const stat = sec.querySelector('.run-stat');
    const state = r.active ? 'running' : 'idle';
    if (stat) {
      stat.className = 'run-stat tone-' + state;
      stat.innerHTML = ICON[state] + (r.active ? '运行中' : '已结束');
    }
    const t = sec.querySelector('.run-time');
    if (t) { t.textContent = '开始 ' + r.started + ' · ' + (r.active ? '运行中' : ('日志更新 ' + (r.ended || '—'))); }
  });
}

async function loadRuns(rebuild) {
  if (!CUR) { return; }
  const issue = CUR;
  try {
    const res = await fetch('/api/runs/' + encodeURIComponent(issue));
    if (!res.ok) {
      if (rebuild && CUR === issue) { dbodyEl.innerHTML = "<div class='dnote'>无法读取运行记录</div>"; }
      return;
    }
    const data = await res.json();
    if (CUR !== issue) { return; }
    const next = data.runs || [];
    const changed = next.map(function (r) { return r.short; }).join(',') !== RUNS.map(function (r) { return r.short; }).join(',');
    RUNS = next;
    AGENT = data.agent || null;
    TROUBLE = data.trouble || null;
    refreshCard();
    if (rebuild || changed) {
      renderRuns();
      if (!LOG && PENDING_RUN && RUNS.some(function (r) { return r.short === PENDING_RUN; })) {
        openLog(PENDING_RUN);
        PENDING_RUN = null;
      } else if (!LOG && RUNS.length && RUNS[0].active) {
        openLog(RUNS[0].short);
      }
    } else {
      updateRunStates();
      updateNoteBox();
    }
  } catch (err) { }
}
setInterval(function () { if (CUR) { loadRuns(false); } }, 5000);

function openLog(short) {
  if (LOG && LOG.short === short) { return; }
  if (LOG) { closeLog(LOG.short); }
  LOG = {
    short: short, offset: -1, live: true, following: true, busy: false,
    dead: false, skipped: 0, src: Math.random().toString(36).slice(2, 10)
  };
  const sec = document.getElementById('log-' + short);
  if (sec) { sec.hidden = false; }
  const lb = document.getElementById('lb-' + short);
  if (lb) { lb.innerHTML = ''; }
  const lm = document.getElementById('lm-' + short);
  if (lm) { lm.textContent = ''; lm.className = 'log-meta'; }
  const btn = document.querySelector(".logbtn[data-log='" + short + "']");
  if (btn) { btn.setAttribute('aria-pressed', 'true'); }
  updateFollowBtn();
  pollLog();
}

function closeLog(short) {
  const sec = document.getElementById('log-' + short);
  if (sec) { sec.hidden = true; }
  const btn = document.querySelector(".logbtn[data-log='" + short + "']");
  if (btn) { btn.setAttribute('aria-pressed', 'false'); }
}

function updateFollowBtn() {
  if (!LOG) { return; }
  const b = document.getElementById('fb-' + LOG.short);
  if (!b) { return; }
  b.textContent = LOG.following ? '跟随中' : '恢复跟随';
  b.setAttribute('aria-pressed', String(!!LOG.following));
}

function trimLog(box) {
  if (!box) { return; }
  const msgs = box.querySelectorAll('.msg');
  const excess = msgs.length - MAX_MSGS;
  for (let i = 0; i < excess; i++) { msgs[i].remove(); }
  if (excess > 0 && LOG) { LOG.skipped += excess; }
}

async function pollLog() {
  if (!LOG || LOG.busy || LOG.dead) { return; }
  const short = LOG.short;
  const issue = CUR;
  LOG.busy = true;
  try {
    const sent = LOG.offset;
    const url = '/api/run/' + encodeURIComponent(issue) + '/' + short
      + '?src=' + LOG.src + (sent >= 0 ? '&offset=' + sent : '');
    const res = await fetch(url);
    if (!LOG || LOG.short !== short) { return; }
    const lb = document.getElementById('lb-' + short);
    const lm = document.getElementById('lm-' + short);
    if (res.status === 404) {
      LOG.dead = true; LOG.live = false;
      if (lm) { lm.textContent = '记录已被清理'; lm.className = 'log-meta warn'; }
      return;
    }
    if (!res.ok) { return; }
    const data = await res.json();
    if (!LOG || LOG.short !== short) { return; }
    if (data.reset) {
      if (lb) { lb.innerHTML = ''; }
      LOG.offset = -1;
      return;
    }
    const first = sent === -1;
    if (!first && data.start !== sent && lb) {
      lb.insertAdjacentHTML('beforeend', "<div class='ln mark err'>… 记录已截断，从当前位置继续 …</div>");
      LOG.skipped = 0;
    }
    LOG.offset = data.offset;
    LOG.live = data.active || !data.eof;
    if (first && data.truncated && lb) {
      lb.insertAdjacentHTML('beforeend', "<div class='ln mark'>… 更早的记录已省略，仅显示最近部分 …</div>");
    }
    if (data.ops && data.ops.length && lb) {
      for (const op of data.ops) {
        const el = lb.querySelector("[data-bid='" + op.id + "']");
        if (op.op === 'del') { if (el) { el.remove(); } }
        else if (el) { el.outerHTML = op.html; }
        else { lb.insertAdjacentHTML('beforeend', op.html); }
      }
      trimLog(lb);
    } else if (data.html && lb) {
      lb.insertAdjacentHTML('beforeend', data.html);
      trimLog(lb);
    }
    if (LOG.following && lb) { lb.scrollTop = lb.scrollHeight; }
    if (lm) {
      lm.textContent = (data.active ? '实时更新中' : '已结束')
        + ' · ' + fmtKB(data.size)
        + (LOG.skipped ? ' · 顶部已省略 ' + LOG.skipped + ' 条' : '');
      lm.className = 'log-meta' + (data.active ? ' live' : '');
    }
  } catch (err) { }
  finally { if (LOG && LOG.short === short) { LOG.busy = false; } }
}
setInterval(function () { if (LOG && LOG.live) { pollLog(); } }, 1000);

dheadEl.addEventListener('click', function (e) {
  if (e.target.closest('#dclose')) { closeDrawer(); }
});

dbodyEl.addEventListener('click', function (e) {
  if (e.target.closest('[data-sync-check]')) { refreshIssues(true); return; }
  if (e.target.closest('[data-sync-dismiss]')) {
    PENDING_OPERATIONS.delete(CUR); refreshIssues(true).then(renderActions); return;
  }
  if (e.target.closest('#dclose')) { closeDrawer(); return; }
  const iv = e.target.closest('[data-intervene]');
  if (e.target.closest('[data-rework]')) { reworkAction(); return; }
  if (iv) { interveneAction(iv.getAttribute('data-intervene')); return; }
  const act = e.target.closest('[data-act]');
  if (act) { labelAction(act.getAttribute('data-act'), act.getAttribute('data-label')); return; }
  const fb = e.target.closest('[data-follow]');
  if (fb) {
    if (LOG) {
      LOG.following = !LOG.following;
      updateFollowBtn();
      if (LOG.following) {
        const lb = document.getElementById('lb-' + LOG.short);
        if (lb) { lb.scrollTop = lb.scrollHeight; }
      }
    }
    return;
  }
  const btn = e.target.closest('[data-log]');
  if (btn) {
    const short = btn.getAttribute('data-log');
    if (LOG && LOG.short === short) { closeLog(short); LOG = null; }
    else { openLog(short); }
  }
});

dbodyEl.addEventListener('wheel', function (e) {
  const lb = e.target.closest('.log-body');
  if (lb && LOG && LOG.following && e.deltaY < 0) {
    LOG.following = false;
    updateFollowBtn();
  }
}, { passive: true });

document.addEventListener('scroll', function (e) {
  const lb = e.target;
  if (!lb || !lb.classList || !lb.classList.contains('log-body')) { return; }
  if (LOG && LOG.following && lb.scrollTop + lb.clientHeight < lb.scrollHeight - 40) {
    LOG.following = false;
    updateFollowBtn();
  }
}, true);

let VIEW = 'active', COMPLETED = [], COMPLETED_DAY = '', COMPLETED_WARNING = '';
let COMPLETED_LOADING = false, COMPLETED_LOADED_AT = 0;

function renderCompleted() {
  const list = document.getElementById('completed-list');
  if (!list) { return; }
  const q = (document.getElementById('q').value || '').trim().toLowerCase().replace(/^#/, '');
  const visible = COMPLETED.filter(function (issue) {
    return (VIEW !== 'today' || issue.day === COMPLETED_DAY)
      && (!q || ('gh-' + issue.number + ' ' + issue.title).toLowerCase().includes(q));
  });
  document.getElementById('completed-status').textContent = COMPLETED_LOADING ? '正在读取已完成 Issue…' : COMPLETED_WARNING;
  document.getElementById('completed-count').textContent = (VIEW === 'today' ? COMPLETED_DAY + ' · 今日' : '历史')
    + '已完成 ' + visible.length + ' 个 Issue（北京时间）';
  list.innerHTML = visible.map(function (issue) {
    const name = 'GH-' + issue.number;
    const result = issue.merged_pr ? '已合并' : '已关闭';
    const pr = issue.merged_pr || {};
    return "<article class='bcard idle completed-card' tabindex='0' role='button' data-issue='" + name
      + "' data-title='" + escHtml(issue.title) + "' data-url='" + escHtml(issue.url)
      + "' data-state='done' data-state-text='" + result + "' data-pr='" + escHtml(pr.url || '')
      + "' data-pr-num='" + escHtml(String(pr.number || '')) + "' data-closed='1' data-completed-at='"
      + escHtml(issue.completed_at) + "' data-description='" + escHtml(issue.body || '') + "'>"
      + "<div class='completed-card-head'><span class='bid'>" + name + "</span><span class='completed-badge'>" + result + "</span></div>"
      + "<div class='btitle'>" + escHtml(issue.title) + "</div><div class='completed-time'>完成于 "
      + escHtml(issue.completed_at) + (pr.number ? ' · PR #' + escHtml(String(pr.number)) : '')
      + "</div><div class='completed-hint'>查看详情与运行日志 ›</div></article>";
  }).join('') || (COMPLETED_LOADING ? '' : "<div class='empty-col'>" + (COMPLETED_WARNING ? '暂无可显示数据，请重试。' : '当前筛选没有已完成的 Issue。') + "</div>");
  markSel();
}

async function loadCompleted(force) {
  if (COMPLETED_LOADING) { return; }
  if (!force && Date.now() - COMPLETED_LOADED_AT < 60000) { renderCompleted(); return; }
  COMPLETED_LOADING = true;
  renderCompleted();
  try {
    const res = await fetch('/api/completed');
    if (!res.ok) { throw new Error('HTTP ' + res.status); }
    const data = await res.json();
    COMPLETED = data.issues || [];
    COMPLETED_DAY = data.today || '';
    COMPLETED_WARNING = data.warning || '';
    COMPLETED_LOADED_AT = Date.now();
  } catch (err) {
    COMPLETED_WARNING = '已完成 Issue 读取失败，请点击刷新重试。' + (COMPLETED.length ? ' 当前展示上次数据。' : '');
  } finally {
    COMPLETED_LOADING = false;
    renderCompleted();
  }
}

function switchView(view) {
  if (!['active', 'today', 'history'].includes(view)) { view = 'active'; }
  VIEW = view;
  closeDrawer();
  document.getElementById('board').hidden = view !== 'active';
  document.getElementById('completed-panel').hidden = view === 'active';
  document.getElementById('showEmpty').closest('label').hidden = view !== 'active';
  document.querySelectorAll('[data-view]').forEach(function (button) {
    button.classList.toggle('on', button.dataset.view === view);
    button.setAttribute('aria-pressed', String(button.dataset.view === view));
  });
  document.querySelector('.head h1').textContent = view === 'active' ? 'Issue 工作台' : (view === 'today' ? '今日已完成 Issue' : '历史已完成 Issue');
  const note = document.querySelector('.head .note');
  if (!note.dataset.activeText) { note.dataset.activeText = note.textContent; }
  note.textContent = view === 'active' ? note.dataset.activeText : '按 Issue 完成时间展示，点击卡片在当前页查看详情和运行日志。';
  document.getElementById('q').placeholder = view === 'active' ? '搜索标题、仓库或 #编号' : '搜索已完成 Issue 标题或编号';
  document.getElementById('sum').hidden = view !== 'active';
  const url = new URL(location.href);
  url.searchParams.delete('scope');
  if (view === 'active') { url.searchParams.delete('view'); }
  else { url.searchParams.set('view', view); }
  history.replaceState(null, '', url);
  if (view !== 'active') { return loadCompleted(false); }
}

document.querySelectorAll('[data-view]').forEach(function (button) {
  button.addEventListener('click', function () { switchView(button.dataset.view); });
});
document.getElementById('completed-refresh').addEventListener('click', function () { loadCompleted(true); });
document.getElementById('completed-list').addEventListener('click', function (e) {
  const card = e.target.closest('[data-issue]');
  if (card) { openIssue(card.dataset.issue); }
});
document.getElementById('completed-list').addEventListener('keydown', function (e) {
  const card = e.target.closest('[data-issue]');
  if (card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openIssue(card.dataset.issue); }
});
setInterval(function () { if (VIEW !== 'active') { loadCompleted(false); } }, 60000);

let ISSUES_READY = false;
function applyFilter() {
  if (!ISSUES_READY && VIEW === 'active') { return; }
  if (VIEW !== 'active') { renderCompleted(); }
  const q = (document.getElementById('q').value || '').trim().toLowerCase().replace(/^#/, '');
  COLS.forEach(function (k) {
    const body = document.getElementById('col-' + k);
    if (!body) { return; }
    let visible = 0;
    body.querySelectorAll('.bcard').forEach(function (c) {
      const hit = !q || (c.getAttribute('data-search') || '').indexOf(q) >= 0;
      c.hidden = !hit;
      if (hit) { visible += 1; }
    });
    const cc = document.getElementById('cc-' + k);
    if (cc) { cc.textContent = visible; }
    const empty = body.querySelector('.empty-col');
    if (visible === 0) {
      const text = q ? '没有匹配的 Issue' : EMPTY[k];
      if (!empty) { body.insertAdjacentHTML('beforeend', "<div class='empty-col'>" + text + "</div>"); }
      else { empty.textContent = text; }
    } else if (empty) {
      empty.remove();
    }
  });
  applyShowEmpty();
}

function applyShowEmpty() {
  COLS.forEach(function (k) {
    const col = document.querySelector(".col[data-col='" + k + "']");
    if (!col) { return; }
    let visible = 0;
    col.querySelectorAll('.bcard').forEach(function (c) { if (!c.hidden) { visible += 1; } });
    const empty = visible === 0;
    col.classList.toggle('is-empty', empty);
    col.hidden = !SHOW_EMPTY && empty;
  });
}

let ISSUES_REQUEST = null;
function refreshIssues(force = false) {
  if (ISSUES_REQUEST) {
    // A write must be followed by a read started after that write, even when
    // an older periodic read is still in flight.
    return force === true
      ? ISSUES_REQUEST.then(() => refreshIssues(true))
      : ISSUES_REQUEST;
  }
  ISSUES_REQUEST = fetchIssues().finally(() => { ISSUES_REQUEST = null; });
  return ISSUES_REQUEST;
}
async function fetchIssues(retried = false) {
  try {
    const res = await fetch('/api/issues');
    if (res.status === 409 && !retried) { return fetchIssues(true); }
    if (!res.ok) { throw new Error('HTTP ' + res.status); }
    const data = await res.json();
    confirmOperations(data);
    ISSUES_READY = true;
    WRITABLE = data.writable === true;
    for (const k of COLS) {
      const body = document.getElementById('col-' + k);
      if (body) { body.innerHTML = data.cols[k] || ''; }
    }
    const health = document.getElementById('health');
    if (health) {
      health.textContent = '运行中 ' + (data.counts.running || 0) + ' · 更新于 ' + data.updated;
    }
    const all = document.getElementById('chipAll');
    if (all) { all.textContent = data.counts.total; }
    const sum = document.getElementById('sum');
    if (sum) { sum.textContent = data.summary || sum.textContent; }
    markSel();
    applyFilter();
    renderPendingCards();
    if (CUR) { refreshCard(); updateNoteBox(); }
  } catch (err) {
    const health = document.getElementById('health');
    if (health) { health.textContent = '数据加载失败，稍后自动重试'; }
    const sum = document.getElementById('sum');
    if (sum && sum.textContent === '正在加载任务…') { sum.textContent = '数据加载失败，稍后自动重试'; }
  }
}
setInterval(refreshIssues, 15000);

document.getElementById('q').addEventListener('input', applyFilter);
document.getElementById('showEmpty').addEventListener('change', function (e) {
  SHOW_EMPTY = e.target.checked;
  localStorage.setItem('sb-show-empty', SHOW_EMPTY ? '1' : '0');
  applyShowEmpty();
});
document.addEventListener('keydown', function (e) {
  if (e.key === '/' && !(e.target instanceof HTMLInputElement) && !(e.target instanceof HTMLTextAreaElement)) {
    e.preventDefault();
    document.getElementById('q').focus();
  }
});
applyFilter();

const initialParams = new URLSearchParams(location.search);
const fromUrl = initialParams.get('issue');
const initialView = initialParams.get('view');
if (initialView && initialView !== 'active') {
  Promise.resolve(switchView(initialView === 'completed' ? 'history' : initialView)).then(function () {
    if (fromUrl) { openIssue(fromUrl); }
  });
}
refreshIssues().then(function () {
  if (fromUrl && (!initialView || initialView === 'active')) { openIssue(fromUrl); }
});
