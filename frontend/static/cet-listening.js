/* 四六级卡点训练 · 前端模块
 *
 * 设计原则（按施工计划 §1、§4）：
 *   - 只补新增功能必需的控件，不重做 UI、不改原导航与样式
 *   - 独立文件挂载，尽量少改 workspace.html（5080 行）
 *   - 空格只标记不暂停；输入框/按钮/弹窗里的空格不抢
 *   - 标记颜色配文字，不只靠颜色识别
 *
 * 依赖：workspace.html 暴露的 window.__ws（本模块自己取）
 */
(function () {
  'use strict';

  // ── 配置 ────────────────────────────────────────────────
  const CFG = {
    // 标记吸附的回溯窗口：用户通常在「没跟上之后」才按键，
    // 所以候选句要往前找。这个值需实际试听调整（计划 §4）。
    lookbackSec: Number(window.CET_LOOKBACK_SEC || 2.5),
    apiBase: '/api/cet',
    // 轮询恢复用
    storageKey: 'cet.session.v1',
  };

  // ── 状态 ────────────────────────────────────────────────
  const S = {
    materialId: null,
    materialVersion: null,
    sentences: [],
    questions: [],
    answers: {},
    attempt: null,
    marks: [],
    practice: null,
    sessionId: null,
    listening: false,     // 是否处于「练习标记模式」
    lastMarkAt: 0,        // 防连发
  };

  // ── 工具 ────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const fmt = (sec) => {
    const s = Math.max(0, Number(sec) || 0);
    const m = Math.floor(s / 60);
    const r = s - m * 60;
    return `${m}:${r.toFixed(2).padStart(5, '0')}`;
  };
  const uid = (p) => `${p}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;

  function sessionId() {
    if (!S.sessionId) {
      S.sessionId = sessionStorage.getItem(CFG.storageKey) || uid('cet-session');
      sessionStorage.setItem(CFG.storageKey, S.sessionId);
    }
    return S.sessionId;
  }

  async function api(method, path, body) {
    const opt = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opt.body = JSON.stringify(body);
    const res = await fetch(CFG.apiBase + path, opt);
    let data = null;
    try { data = await res.json(); } catch (_) { /* 允许空响应 */ }
    if (!res.ok) {
      const err = new Error((data && (data.detail?.message || data.detail?.code)) || `HTTP ${res.status}`);
      err.status = res.status;
      err.code = data && data.detail && data.detail.code;
      err.payload = data;
      throw err;
    }
    return data;
  }

  // 取工作区暴露的媒体元素与句子数据
  function media() {
    return document.getElementById('local-media-player');
  }
  function workspaceData() {
    // workspace.html 里 lessonData / segments 都是模块内变量，
    // 这里通过 DOM 与已有全局函数间接获取。
    return {
      lessonId: (location.pathname.match(/\/workspace\/(\d+)/) || [])[1],
      currentTime: media() ? media().currentTime : 0,
    };
  }

  // ── UI 挂载 ─────────────────────────────────────────────
  function mount() {
    const host = $('.learning-controls');
    if (!host || $('#cet-toolbar')) return;

    const bar = el('div', 'cet-toolbar');
    bar.id = 'cet-toolbar';

    // 进入/退出练习标记模式
    const toggle = el('button', 'cet-btn', '开始卡点练习');
    toggle.id = 'cet-toggle-mode';
    toggle.onclick = () => setListening(!S.listening);
    bar.appendChild(toggle);

    // 状态提示
    const hint = el('span', 'cet-hint', '');
    hint.id = 'cet-hint';
    bar.appendChild(hint);

    host.appendChild(bar);

    // 标记列表容器：放在字幕卡下方
    const card = $('.subtitle-card');
    if (card && !$('#cet-marks')) {
      const box = el('div', 'cet-marks');
      box.id = 'cet-marks';
      box.hidden = true;
      card.appendChild(box);
    }

    // 题目面板：放在侧栏导航标签里（复用原侧栏）
    mountQuestionPanel();

    // 恢复上次进度
    restore();
  }

  function setListening(on) {
    S.listening = on;
    // 关键：点完按钮后焦点会留在按钮上，之后按空格会被浏览器当成「点击按钮」，
    // 我们的标记就永远收不到。进入练习模式时主动把焦点移走。
    if (on && document.activeElement && document.activeElement.blur) {
      document.activeElement.blur();
    }
    const btn = $('#cet-toggle-mode');
    const hint = $('#cet-hint');
    if (btn) btn.textContent = on ? '结束卡点练习' : '开始卡点练习';
    if (btn) btn.classList.toggle('active', on);
    if (hint) {
      hint.textContent = on
        ? '播放时按空格标记「没听清」的位置（不会暂停）'
        : '';
    }
    document.body.classList.toggle('cet-listening', on);
  }

  // ── 空格标记 ────────────────────────────────────────────
  function isTypingTarget(t) {
    if (!t) return false;
    const tag = (t.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    if (t.isContentEditable) return true;
    // 弹窗/对话框里的按键不抢
    if (t.closest && t.closest('[role="dialog"], .modal, .subtitle-error-modal')) return true;
    return false;
  }

  function onKeyDown(e) {
    if (e.code !== 'Space') return;
    if (!S.listening) return;
    if (isTypingTarget(e.target)) return;
    // 按钮聚焦时空格是「点击」，不抢 —— 但练习模式下用户可能刚点过按钮，
    // 此时按钮仍在聚焦状态。已在 setListening 里 blur，这里再兜一层：
    // 只要不是 cet 自己的按钮，就不抢。
    const onButton = (e.target.tagName || '').toLowerCase() === 'button';
    const isCetButton = onButton && e.target.closest && e.target.closest('.cet-btn, .cet-btn-sm');
    if (onButton && !isCetButton) return;
    if (e.repeat) return;                  // 忽略长按连发

    const m = media();
    if (!m) return;

    // 暂停状态下空格恢复播放，不新增标记（计划 §4）
    if (m.paused) {
      e.preventDefault();
      m.play();
      flashHint('继续播放');
      return;
    }

    e.preventDefault();
    // 防连发：300ms 内只认一次
    const now = Date.now();
    if (now - S.lastMarkAt < 300) return;
    S.lastMarkAt = now;

    createMark(m.currentTime);
  }

  async function createMark(pressedAtSec) {
    if (!S.materialId) return;
    try {
      const mark = await api('POST', '/marks', {
        materialId: S.materialId,
        materialVersion: S.materialVersion,
        listeningSessionId: sessionId(),
        pressedAtSec: Number(pressedAtSec.toFixed(3)),
      });
      S.marks.push(mark);
      renderMarks();
      flashHint(`已在 ${fmt(mark.pressedAtSec)} 标记`);
    } catch (err) {
      flashHint(`标记失败：${err.message}（可重试）`, true);
    }
  }

  function flashHint(text, isError) {
    const hint = $('#cet-hint');
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle('error', !!isError);
    clearTimeout(flashHint._t);
    flashHint._t = setTimeout(() => {
      hint.textContent = S.listening ? '播放时按空格标记「没听清」的位置（不会暂停）' : '';
      hint.classList.remove('error');
    }, 2600);
  }

  // ── 标记渲染（三态：待确认 / 待练 / 本轮已练）──────────
  function markState(m) {
    if (m.confirmedSentenceId) {
      return S.practice && S.practice.markId === m.markId && S.practice.status === 'complete'
        ? 'done' : 'ready';
    }
    return 'pending';
  }

  function renderMarks() {
    const box = $('#cet-marks');
    if (!box) return;
    box.innerHTML = '';
    if (!S.marks.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const title = el('div', 'cet-marks-title', `卡点标记（${S.marks.length}）`);
    box.appendChild(title);

    S.marks.forEach((m) => {
      const st = markState(m);
      const row = el('div', `cet-mark cet-mark-${st}`);

      const when = el('span', 'cet-mark-time', fmt(m.pressedAtSec));
      row.appendChild(when);

      const label = { pending: '待确认', ready: '待练', done: '本轮已练' }[st];
      const badge = el('span', `cet-mark-badge cet-mark-badge-${st}`, label);
      row.appendChild(badge);

      const sent = S.sentences.find((s) => s.sentenceId === m.confirmedSentenceId
        || s.sentenceId === m.suggestedSentenceId);
      if (sent) {
        const q = el('span', 'cet-mark-sentence', sent.text);
        row.appendChild(q);
      }

      const acts = el('span', 'cet-mark-actions');

      // 回听
      const play = el('button', 'cet-btn-sm', '回听');
      play.onclick = () => replayMark(m);
      acts.appendChild(play);

      // 确认/调整
      if (st === 'pending') {
        const conf = el('button', 'cet-btn-sm', '确认这句');
        conf.onclick = () => confirmMark(m);
        acts.appendChild(conf);

        const prev = el('button', 'cet-btn-sm', '前一句');
        prev.onclick = () => adjustMark(m, -1);
        acts.appendChild(prev);

        const next = el('button', 'cet-btn-sm', '后一句');
        next.onclick = () => adjustMark(m, 1);
        acts.appendChild(next);
      }

      if (st === 'ready') {
        const start = el('button', 'cet-btn-sm', '开始带练');
        start.onclick = () => startPractice(m);
        acts.appendChild(start);
      }

      row.appendChild(acts);
      box.appendChild(row);
    });
  }

  function replayMark(m) {
    const sid = m.confirmedSentenceId || m.suggestedSentenceId;
    const sent = S.sentences.find((s) => s.sentenceId === sid);
    const md = media();
    if (!sent || !md) return;
    md.currentTime = sent.startSec;
    md.play();
    const stopAt = sent.endSec;
    const guard = () => {
      if (md.currentTime >= stopAt) {
        md.pause();
        md.removeEventListener('timeupdate', guard);
      }
    };
    md.addEventListener('timeupdate', guard);
  }

  async function confirmMark(m, sentenceId) {
    const sid = sentenceId || m.confirmedSentenceId || m.suggestedSentenceId;
    try {
      const updated = await api('PATCH', `/marks/${m.markId}`, { confirmedSentenceId: sid });
      Object.assign(m, updated);
      renderMarks();
      flashHint('已确认卡点句子');
    } catch (err) {
      if (err.code === 'ATTEMPT_REQUIRED') {
        flashHint('请先交卷，再确认卡点句子', true);
      } else {
        flashHint(`确认失败：${err.message}`, true);
      }
    }
  }

  function adjustMark(m, delta) {
    const cur = m.confirmedSentenceId || m.suggestedSentenceId;
    let idx = S.sentences.findIndex((s) => s.sentenceId === cur);
    if (idx < 0) idx = 0;
    idx = Math.min(S.sentences.length - 1, Math.max(0, idx + delta));
    const target = S.sentences[idx];
    if (target) confirmMark(m, target.sentenceId);
  }

  // ── 题目面板 ────────────────────────────────────────────
  function mountQuestionPanel() {
    const side = $('.side-column');
    if (!side || $('#cet-questions')) return;

    const wrap = el('section', 'cet-panel');
    wrap.id = 'cet-questions';
    wrap.innerHTML = `
      <div class="cet-panel-head">
        <strong>四六级题目</strong>
        <span id="cet-attempt-state" class="cet-panel-state">未交卷</span>
      </div>
      <div id="cet-question-list" class="cet-question-list"></div>
      <div class="cet-panel-foot">
        <button id="cet-submit" class="cet-btn">交卷</button>
      </div>
    `;
    side.insertBefore(wrap, side.firstChild);
    $('#cet-submit').onclick = submitAttempt;
  }

  function renderQuestions() {
    const list = $('#cet-question-list');
    if (!list) return;
    list.innerHTML = '';
    if (!S.questions.length) {
      list.appendChild(el('div', 'cet-empty', '本材料暂无题目'));
      return;
    }
    S.questions.forEach((q) => {
      const item = el('div', 'cet-q');
      item.appendChild(el('div', 'cet-q-stem', `第 ${q.number} 题　${q.stem}`));
      const opts = el('div', 'cet-q-opts');
      q.options.forEach((opt, i) => {
        const b = el('button', 'cet-opt', `${'ABCDE'[i]}. ${opt}`);
        b.onclick = () => {
          S.answers[q.questionId] = i;
          opts.querySelectorAll('.cet-opt').forEach((x) => x.classList.remove('sel'));
          b.classList.add('sel');
        };
        opts.appendChild(b);
      });
      item.appendChild(opts);
      list.appendChild(item);
    });
  }

  async function submitAttempt() {
    if (!S.questions.length) return;
    const answers = Object.entries(S.answers).map(([questionId, choice]) => ({ questionId, choice }));
    if (!answers.length) { flashHint('先选几道题再交卷', true); return; }
    try {
      const at = await api('POST', '/attempts', {
        materialId: S.materialId,
        materialVersion: S.materialVersion,
        listeningSessionId: sessionId(),
        answers,
      });
      S.attempt = at;
      renderAttemptResult(at);
      const st = $('#cet-attempt-state');
      if (st) { st.textContent = `已交卷 · 得分 ${at.score}`; st.classList.add('done'); }
      flashHint('已交卷，可以确认卡点句子了');
      save();
    } catch (err) {
      flashHint(`交卷失败：${err.message}`, true);
    }
  }

  function renderAttemptResult(at) {
    const list = $('#cet-question-list');
    if (!list) return;
    list.innerHTML = '';
    (at.results || []).forEach((r) => {
      const cls = r.correct === true ? 'ok' : (r.correct === false ? 'no' : 'unk');
      const item = el('div', `cet-q-result cet-q-${cls}`);
      const letters = 'ABCDE';
      const pick = r.choice == null ? '-' : letters[r.choice];
      const right = r.answer == null ? '参考答案未知' : `正确 ${letters[r.answer]}`;
      const verdict = r.correct === true ? '答对' : (r.correct === false ? '答错' : '不判对错');
      item.appendChild(el('div', 'cet-q-stem', `第 ${r.number} 题　你选 ${pick} · ${right} · ${verdict}`));
      if (r.evidenceRef) {
        const q = el('div', 'cet-q-evidence',
          `原声证据 [${fmt(r.evidenceRef.startSec)}–${fmt(r.evidenceRef.endSec)}] ${r.evidenceRef.quote}`);
        item.appendChild(q);
      }
      list.appendChild(item);
    });
  }

  // ── 带练 ────────────────────────────────────────────────
  async function startPractice(m) {
    try {
      const pr = await api('POST', '/practices', { markId: m.markId });
      S.practice = pr;
      renderPractice();
      renderMarks();
    } catch (err) {
      if (err.code === 'ATTEMPT_REQUIRED') {
        flashHint('请先交卷，再开始带练', true);
      } else {
        flashHint(`开始带练失败：${err.message}`, true);
      }
    }
  }

  function renderPractice() {
    let box = $('#cet-practice');
    if (!box) {
      const side = $('.side-column');
      if (!side) return;
      box = el('section', 'cet-panel');
      box.id = 'cet-practice';
      side.insertBefore(box, $('#cet-questions'));
    }
    box.innerHTML = '';
    if (!S.practice) { box.hidden = true; return; }
    box.hidden = false;

    const head = el('div', 'cet-panel-head');
    head.appendChild(el('strong', null, '卡点带练'));
    box.appendChild(head);

    const fbRow = el('div', 'cet-feedback');
    [['sound', '没听清'], ['meaning', '不懂意思'], ['heard', '听懂了']].forEach(([fb, label]) => {
      const b = el('button', 'cet-btn', label);
      b.onclick = () => sendFeedback(fb, label);
      fbRow.appendChild(b);
    });
    box.appendChild(fbRow);

    const out = el('div', 'cet-practice-out');
    out.id = 'cet-practice-out';
    box.appendChild(out);

    const saveBtn = el('button', 'cet-btn', '保存本轮');
    saveBtn.onclick = finishPractice;
    box.appendChild(saveBtn);
  }

  async function sendFeedback(feedback, label) {
    if (!S.practice) return;
    const out = $('#cet-practice-out');
    if (out) out.textContent = `反馈「${label}」处理中…`;
    try {
      const r = await api('POST', `/practices/${S.practice.practiceId}/next`, {
        feedback,
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
      });
      S.practice = { ...S.practice, ...r, currentStepId: r.currentStepId || S.practice.currentStepId };
      const step = r.step || {};
      if (out) {
        out.textContent = `反馈「${label}」→ 动作：${step.action || r.currentAction || '已更新'}`;
        out.classList.add('ok');
      }
      save();
    } catch (err) {
      if (out) {
        out.textContent = err.code === 'STALE_RESPONSE'
          ? '这一步已过期，请刷新后重试'
          : `反馈失败：${err.message}`;
        out.classList.add('error');
      }
    }
  }

  async function finishPractice() {
    if (!S.practice) return;
    try {
      const r = await api('POST', `/practices/${S.practice.practiceId}/finish`, {
        feedback: S.practice.latestFeedback || 'sound',
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
      });
      S.practice = { ...S.practice, ...r };
      flashHint('本轮已保存');
      renderMarks();
      save();
    } catch (err) {
      flashHint(`保存失败：${err.message}`, true);
    }
  }

  // ── 恢复 ────────────────────────────────────────────────
  function save() {
    try {
      sessionStorage.setItem('cet.state.v1', JSON.stringify({
        materialId: S.materialId,
        attemptId: S.attempt && S.attempt.attemptId,
        practiceId: S.practice && S.practice.practiceId,
      }));
    } catch (_) { /* 隐私模式可能失败，不阻塞 */ }
  }

  async function restore() {
    // 从后端按会话恢复标记（这是「保存并重开」的关键）
    if (!S.materialId) return;
    try {
      const r = await api('GET', `/marks?materialId=${encodeURIComponent(S.materialId)}`
        + `&listeningSessionId=${encodeURIComponent(sessionId())}`);
      const items = (r && (r.items || r.marks)) || [];
      if (items.length) {
        S.marks = items;
        renderMarks();
        flashHint(`已恢复 ${items.length} 个卡点标记`);
      }
    } catch (_) { /* 恢复失败不阻塞主流程 */ }
  }

  // ── 初始化 ──────────────────────────────────────────────
  async function boot() {
    // 从页面里找出当前课程对应的四六级材料
    const ws = workspaceData();
    if (!ws.lessonId) return;

    try {
      // 按 lesson id 精确反查：只有四六级材料才挂载卡点控件。
      // 不用标题匹配 —— 实测会命中标题相近的其他课程（v2-15）。
      const detail = await api('GET', `/materials/by-lesson/${ws.lessonId}`);

      S.materialId = detail.materialId;
      S.materialVersion = detail.materialVersion;
      S.sentences = detail.sentences || [];

      const q = await api('GET', `/materials/${encodeURIComponent(S.materialId)}/questions`
        + `?version=${encodeURIComponent(S.materialVersion)}`);
      S.questions = (q && (q.questions || q.items)) || [];

      mount();
      renderQuestions();
      renderMarks();
    } catch (err) {
      if (err.status === 404) {
        // 不是四六级材料：静默跳过，不影响原页面
        return;
      }
      console.warn('[cet] 初始化失败', err);
    }
  }

  document.addEventListener('keydown', onKeyDown, true);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 暴露给调试与测试
  window.__cet = { S, api, setListening, createMark, confirmMark, submitAttempt };
})();
