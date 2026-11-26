const $ = (id) => document.getElementById(id);

let currentTask = null;
let inputMode = 'demo';
let busy = false;

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `请求失败 (${resp.status})`);
  return data;
}

function setBusy(state) {
  busy = state;
  $('start-btn').disabled = state;
  $('reply-btn').disabled = state;
}

function showError(msg) {
  const el = $('form-error');
  el.textContent = msg || '';
  el.hidden = !msg;
}

function setInputMode(mode) {
  inputMode = mode;
  $('demo-picker').hidden = mode !== 'demo';
  $('upload-picker').hidden = mode !== 'upload';
  $('tab-demo').className = mode === 'demo' ? 'button active' : 'button subdued';
  $('tab-upload').className = mode === 'upload' ? 'button active' : 'button subdued';
}

function statusBadgeClass(status) {
  if (status === 'completed') return 'badge ok';
  if (status === 'waiting_for_user') return 'badge warn';
  if (status === 'failed') return 'badge err';
  return 'badge';
}

function renderTask(task) {
  currentTask = task;
  $('task-heading').textContent = task.project_id
    ? `任务 ${task.task_id} · 项目 ${task.project_id}`
    : `任务 ${task.task_id}`;
  const st = $('task-status');
  st.textContent = `${task.status.toUpperCase()} · ${task.provider}/${task.model}`;
  st.className = statusBadgeClass(task.status);

  // Missing info warnings
  const missing = task.missing_info || [];
  const mb = $('missing-banner');
  if (missing.length) {
    mb.hidden = false;
    mb.innerHTML = `<strong>缺失/待核实信息 (${missing.length})：</strong><br>` +
      missing.map((m) => `• [${m.flag || m.field}] ${m.message}`).join('<br>');
  } else {
    mb.hidden = true;
  }

  // Pending question & clickable candidates
  const pending = task.pending_question;
  const pendingPanel = $('pending-panel');
  if (task.status === 'waiting_for_user' && pending) {
    pendingPanel.hidden = false;
    $('pending-question').textContent = pending.question || '请确认后继续当前评审任务：';
    const list = $('candidate-list');
    list.replaceChildren();
    for (const cand of pending.candidates || []) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'candidate-btn';
      if (pending.kind === 'disambiguate_asset') {
        const disp = typeof cand.displacement_m === 'number' ? ` · 位移 ${cand.displacement_m.toFixed(2)}m` : '';
        btn.textContent = `选择 ${cand.tag || cand.guid} (${cand.name || ''}${disp})`;
        btn.addEventListener('click', () => sendReply({ selected_tag: cand.tag, selected_guid: cand.guid }));
      } else {
        btn.textContent = `确认匹配：${cand.tag || cand.name || '候选'} (Rev A → Rev B)`;
        btn.addEventListener('click', () => sendReply({
          reply: '确认匹配并继续',
          confirm_matches: cand.rev_a_guid && cand.rev_b_guid ? [{ rev_a_guid: cand.rev_a_guid, rev_b_guid: cand.rev_b_guid }] : [],
        }));
      }
      list.append(btn);
    }
  } else {
    pendingPanel.hidden = true;
  }

  // Summary
  if (task.error) {
    $('summary-body').textContent = task.error;
  } else if (task.final_summary) {
    $('summary-body').textContent = task.final_summary;
  } else if (task.status === 'waiting_for_user' && pending) {
    $('summary-body').textContent = `等待用户确认：${pending.question}`;
  } else {
    $('summary-body').textContent = '执行中…';
  }

  // Verified artifacts ONLY when files actually exist on disk
  const artifacts = (task.artifacts || []).filter((a) => a.verified && a.download_url);
  const artCard = $('artifacts-card');
  if (artifacts.length > 0) {
    artCard.hidden = false;
    const grid = $('artifact-grid');
    grid.replaceChildren();
    for (const art of artifacts) {
      const a = document.createElement('a');
      a.className = 'artifact-link';
      a.href = art.download_url;
      a.download = art.filename;
      a.textContent = `↓ ${art.label} (${art.filename}, ${(art.size_bytes / 1024).toFixed(1)} KB)`;
      grid.append(a);
    }
    const issues = task.issues_preview || [];
    const wrap = $('issues-wrap');
    if (issues.length) {
      wrap.hidden = false;
      const tbody = $('issues-tbody');
      tbody.replaceChildren();
      for (const row of issues) {
        const tr = document.createElement('tr');
        const dist = row.distance_rev_b_m !== '' && row.distance_rev_b_m !== undefined
          ? `${row.distance_rev_a_m}m → ${row.distance_rev_b_m}m`
          : (row.displacement_m ? `Δ ${row.displacement_m}m` : '—');
        const elevClass = row.elevation_note === '待核实高程' ? 'flag-elev' : '';
        tr.innerHTML = `
          <td><code>${row.issue_id || ''}</code></td>
          <td>${row.category || ''}</td>
          <td><code>${row.asset_tag || ''}</code></td>
          <td>${row.utility_id ? `${row.utility_id} ${row.utility_name || ''}` : '—'}</td>
          <td><code>${dist}</code></td>
          <td class="${elevClass}">${row.elevation_note || '—'}</td>
          <td>${row.summary || ''}</td>
        `;
        tbody.append(tr);
      }
    } else {
      wrap.hidden = true;
    }
  } else {
    artCard.hidden = true;
  }

  // Actual executed tools trace
  const traceEl = $('trace-list');
  traceEl.replaceChildren();
  const calls = task.tool_calls || [];
  if (!calls.length) {
    traceEl.innerHTML = '<p style="color:var(--muted);font-size:12px;">尚未执行工具。</p>';
  } else {
    for (const tc of calls) {
      const box = document.createElement('div');
      box.className = 'tool-step';
      const statusCls = tc.status === 'ok' ? 'badge ok' : (tc.status.includes('needs') ? 'badge warn' : 'badge err');
      box.innerHTML = `
        <div class="tool-step-head">
          <span class="tool-name">#${tc.step} ${tc.name}()</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="${statusCls}">${tc.status}</span>
            <span class="tool-meta">${tc.duration_ms} ms</span>
          </div>
        </div>
        <details class="tool-details">
          <summary>查看输入参数与工具返回 JSON</summary>
          <pre class="tool-json">${escapeHtml(JSON.stringify({ arguments: tc.arguments, result: tc.result }, null, 2))}</pre>
        </details>
      `;
      traceEl.append(box);
    }
  }
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderRecent(tasks) {
  const box = $('recent-tasks');
  box.replaceChildren();
  if (!tasks || !tasks.length) {
    box.textContent = '暂无任务记录';
    return;
  }
  for (const item of tasks) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'button subdued';
    btn.style.cssText = 'text-align:left;font-size:12px;padding:7px 9px;';
    btn.textContent = `${item.task_id} · ${item.status} · ${item.scenario || 'custom'} (${item.artifact_count} files)`;
    btn.addEventListener('click', async () => {
      const full = await api(`/api/agent/tasks/${item.task_id}`);
      renderTask(full);
    });
    box.append(btn);
  }
}

async function refreshConfig() {
  const cfg = await api('/api/agent/config');
  const llm = cfg.llm;
  const badge = $('llm-badge');
  badge.textContent = llm.is_real_llm
    ? `LLM: ${llm.provider} / ${llm.model}`
    : 'Demo Tool Planner (configure LLM_PROVIDER for Ollama/Cloud)';
  badge.className = llm.is_real_llm ? 'badge ok' : 'badge warn';

  const select = $('scenario');
  if (!select.options.length) {
    for (const s of cfg.scenarios) {
      select.append(new Option(s.label, s.id));
    }
  }
  if (!$('instruction').value.trim()) {
    $('instruction').value = cfg.default_prompt;
  }
  if (!cfg.allow_upload) {
    $('tab-upload').disabled = true;
    $('tab-upload').title = '公开演示模式已禁用上传';
  }
  renderRecent(cfg.recent_tasks);
}

async function sendReply(payload) {
  if (!currentTask || busy) return;
  showError('');
  setBusy(true);
  try {
    const updated = await api(`/api/agent/tasks/${currentTask.task_id}/reply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('reply-input').value = '';
    renderTask(updated);
    refreshConfig();
  } catch (err) {
    showError(err.message);
  } finally {
    setBusy(false);
  }
}

$('tab-demo').addEventListener('click', () => setInputMode('demo'));
$('tab-upload').addEventListener('click', () => setInputMode('upload'));

$('agent-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  if (busy) return;
  showError('');
  setBusy(true);
  try {
    let task;
    if (inputMode === 'upload') {
      const fd = new FormData();
      fd.append('instruction', $('instruction').value.trim());
      if ($('file-rev-a').files[0]) fd.append('rev_a', $('file-rev-a').files[0]);
      if ($('file-rev-b').files[0]) fd.append('rev_b', $('file-rev-b').files[0]);
      if ($('file-gis').files[0]) fd.append('gis', $('file-gis').files[0]);
      const ocrs = $('override-crs').value.trim();
      if (ocrs) {
        fd.append('override_crs', ocrs);
        if ($('override-e').value) fd.append('override_eastings', $('override-e').value);
        if ($('override-n').value) fd.append('override_northings', $('override-n').value);
      }
      task = await api('/api/agent/tasks', { method: 'POST', body: fd });
    } else {
      task = await api('/api/agent/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scenario: $('scenario').value,
          instruction: $('instruction').value.trim(),
        }),
      });
    }
    renderTask(task);
    refreshConfig();
  } catch (err) {
    showError(err.message);
  } finally {
    setBusy(false);
  }
});

$('reply-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const reply = $('reply-input').value.trim();
  if (reply) sendReply({ reply });
});

refreshConfig();
