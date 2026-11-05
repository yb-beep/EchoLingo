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
      // S4：用 localStorage。sessionStorage 关标签就丢，
      // 导致重开后按新会话 ID 查不到已保存的卡点（记录还在，入口没了）。
      S.sessionId = localStorage.getItem(CFG.storageKey) || uid('cet-session');
      localStorage.setItem(CFG.storageKey, S.sessionId);
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

    // 今日复习按钮
    const reviewBtn = el('button', 'cet-btn', '今日复习');
    reviewBtn.id = 'cet-review-btn';
    reviewBtn.onclick = startReview;
    bar.appendChild(reviewBtn);

    mountReviewPanel();

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
    md.currentTime = sent.startSeconds;
    md.play();
    const stopAt = sent.endSeconds;
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
      bindPlaybackEvents();
      save();          // F03：不存下来，刷新后就找不回带练
    } catch (err) {
      if (err.code === 'ATTEMPT_REQUIRED') {
        flashHint('请先交卷，再开始带练', true);
      } else {
        flashHint(`开始带练失败：${err.message}`, true);
      }
    }
  }

  // S4：把 practice.currentStep 的内容渲染到输出区（恢复与首次都走这里）
  function renderCurrentStepInto(container) {
    if (!container || !S.practice) return;
    const step = S.practice.currentStep || {};
    const action = step.action || '';
    const data = step.data || {};

    if (action === 'explain') {
      const text = String(data.explanation || '').trim();
      if (text) {
        const box = el('div', 'cet-explain');
        box.appendChild(el('div', 'cet-explain-text', text));
        const phrases = Array.isArray(data.keyPhrases) ? data.keyPhrases : [];
        if (phrases.length) {
          const ph = el('div', 'cet-explain-phrases');
          phrases.forEach((p) => {
            const t = typeof p === 'string' ? p : (p.text || p.phrase || '');
            if (t) ph.appendChild(el('span', 'cet-phrase', t));
          });
          box.appendChild(ph);
        }
        container.appendChild(box);
        return;
      }
      container.appendChild(el('div', 'cet-ai-note',
        '本步骤没有可显示的解释正文；可继续回听或重新反馈。'));
      return;
    }

    if (action === 'transfer' || data.noCandidate) {
      renderTransfer({ currentStep: step, transferStatus: S.practice.transferStatus }, container);
      return;
    }

    if (action === 'slow' || action === 'replay' || action === 'chunk') {
      container.appendChild(el('div', 'cet-ai-note',
        `当前步骤：${ACTION_LABEL[action] || action}${step.speed && step.speed < 1 ? `（${step.speed}x）` : ''}`));
      return;
    }

    if (action === 'finish') {
      container.appendChild(el('div', 'cet-ai-note', '本轮已结束，可保存或开始复习。'));
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

    // N3：练习已完成时不再给反馈入口（否则点了必然报「练习已结束」）
    const finished = S.practice.status === 'complete';
    if (finished) {
      const doneNote = el('div', 'cet-ai-note', '本轮已保存。可以点「今日复习」继续，或回到卡点列表。');
      box.appendChild(doneNote);
    } else {
      const fbRow = el('div', 'cet-feedback');
      [['sound', '没听清'], ['meaning', '不懂意思'], ['heard', '听懂了']].forEach(([fb, label]) => {
        const b = el('button', 'cet-btn', label);
        b.onclick = () => sendFeedback(fb, label);
        fbRow.appendChild(b);
      });
      box.appendChild(fbRow);
    }

    const out = el('div', 'cet-practice-out');
    out.id = 'cet-practice-out';
    box.appendChild(out);

    // S4：恢复时要把当前步骤的内容渲染出来，不能只重建按钮。
    // 原来刷新后解释正文/换句候选项都会消失。
    renderCurrentStepInto(out);

    if (!finished) {
      const saveBtn = el('button', 'cet-btn', '保存本轮');
      saveBtn.onclick = finishPractice;
      box.appendChild(saveBtn);
    }
  }

  // 动作 → 中文说明（计划 §5：两类反馈要给合理不同的帮助）
  const ACTION_LABEL = {
    replay: '原句回放',
    slow: '慢放',
    chunk: '短语分段',
    explain: '解释句意',
    transfer: '换句检验',
  };

  // N2：冲突后从服务端拉取权威状态，让用户能直接继续
  async function resyncPractice() {
    if (!S.practice || !S.practice.practiceId) return;
    try {
      const pr = await api('GET', `/practices/${S.practice.practiceId}`);
      if (pr && pr.practiceId) {
        S.practice = pr;
        renderPractice();
        renderMarks();
        save();
      }
    } catch (_) { /* 同步失败不阻塞，用户仍可手动刷新 */ }
  }

  // S6：请求序号。同一练习的多个请求若在网络上乱序到达，
  // 只允许「最后一次发出」的响应落地，避免旧成功覆盖新步骤。
  let feedbackSeq = 0;

  async function sendFeedback(feedback, label) {
    if (!S.practice) return;
    const out = $('#cet-practice-out');
    const practiceAtSend = S.practice.practiceId;
    const stepAtSend = S.practice.currentStepId;
    const seq = ++feedbackSeq;

    // 请求一开始就显示等待（不是等返回后才启动计时）
    if (out) {
      out.className = 'cet-practice-out';
      out.textContent = `反馈「${label}」处理中…`;
    }
    const waitingNote = el('div', 'cet-ai-note', '正在生成解释…（3 秒后仍无结果可继续回听或保存）');
    // R5：创建后必须挂到 DOM，否则 parentNode 恒为 null，3 秒兜底提示永远不出现
    if (out) out.appendChild(waitingNote);
    const timer = setTimeout(() => {
      if (waitingNote.parentNode) {
        waitingNote.textContent = '仍在等待模型；你可以先回听、慢放、看原文或保存本轮。';
        waitingNote.classList.add('warn');
      }
    }, 3000);

    try {
      const r = await api('POST', `/practices/${S.practice.practiceId}/next`, {
        feedback,
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
      });
      clearTimeout(timer);
      if (waitingNote.parentNode) waitingNote.remove();

      // 跨练习/过期响应过滤：旧练习的结果不能覆盖当前界面（F01）
      // N2：不能只「丢弃旧响应」——那会把唯一能同步新步骤的成功回包也丢掉，
      // 导致用户拿着旧 stepId 反复 409，只能刷新页面。
      // 正确策略：任何成功回包都用来**同步权威状态**；只有「显示」才按序号取舍。
      if (!S.practice || S.practice.practiceId !== practiceAtSend) return;

      // 无论是否最新，先同步服务端返回的真实状态（这是权威的）
      S.practice = { ...S.practice, ...r, currentStepId: r.currentStepId || S.practice.currentStepId };
      save();

      // 只有最新一次请求才负责更新界面文案，避免旧回包闪回旧动作
      if (seq !== feedbackSeq) return;

      S.practice = { ...S.practice, ...r, currentStepId: r.currentStepId || S.practice.currentStepId };
      const step = r.currentStep || r.step || {};
      const action = step.action || r.currentAction || '';

      if (out) {
        out.className = 'cet-practice-out ok';
        out.textContent = `反馈「${label}」→ ${ACTION_LABEL[action] || action || '已更新'}`;
      }

      // 本地动作立刻执行，不等 AI
      if (action === 'replay' || action === 'slow' || action === 'chunk') {
        executeLocalAction(action, step);
      }

      // F01：explain 成功时渲染后端正文，不再固定显示「模型暂不可用」
      if (action === 'explain') {
        renderExplain(r, step);
      }
      // S3：无候选时后端把步骤设成 finish，action 不再是 transfer，
      // 原来只在 action==='transfer' 时渲染，导致「未检验」提示永远不出现。
      // 改为：只要响应里带 noCandidate / transferStatus，就渲染换句区。
      const stepData = step.data || {};
      const isTransferCase = action === 'transfer'
        || stepData.noCandidate === true
        || (r && r.transferStatus === 'no_candidate');
      if (isTransferCase) {
        renderTransfer(r);
      }
      save();
    } catch (err) {
      clearTimeout(timer);
      if (waitingNote.parentNode) waitingNote.remove();
      // S6：失败同样要过滤（身份 + 序号）
      if (!S.practice || S.practice.practiceId !== practiceAtSend) return;
      if (seq !== feedbackSeq) return;
      if (out) {
        out.className = 'cet-practice-out error';
        if (err.code === 'STALE_RESPONSE') {
          // N2：冲突后主动拉取权威状态，让用户能继续，而不是要求刷新
          out.textContent = '这一步已更新，正在同步…';
          await resyncPractice();
          out.textContent = '已同步到最新步骤，可以继续';
        } else if (err.status === 503) {
          out.textContent = '模型尚未配置；仍可使用回听、慢放、原文和保存。';
        } else {
          out.textContent = `反馈失败：${err.message}`;
        }
      }
    }
  }

  // F01：把后端生成正文渲染出来（cet_service 放在 currentStep.data 里）
  function renderExplain(resp, step) {
    const out = $('#cet-practice-out');
    if (!out) return;
    const data = (step && step.data) || (resp && resp.currentStep && resp.currentStep.data) || {};
    const text = String(data.explanation || '').trim();
    const phrases = Array.isArray(data.keyPhrases) ? data.keyPhrases : [];

    if (!text) {
      const note = el('div', 'cet-ai-note warn',
        '模型未返回可用解释；你仍可以回听、慢放、看原文或保存本轮。');
      out.appendChild(note);
      return;
    }
    const box = el('div', 'cet-explain');
    box.appendChild(el('div', 'cet-explain-text', text));
    if (phrases.length) {
      const ph = el('div', 'cet-explain-phrases');
      phrases.forEach((p) => {
        const t = typeof p === 'string' ? p : (p.text || p.phrase || '');
        if (t) ph.appendChild(el('span', 'cet-phrase', t));
      });
      box.appendChild(ph);
    }
    out.appendChild(box);
  }

  // F05：换句检验的候选渲染（本轮先做「没有候选」的明确状态，不假成功）
  function renderTransfer(resp, container) {
    const out = container || $('#cet-practice-out');
    if (!out) return;
    const step = (resp && resp.currentStep) || {};
    const data = step.data || {};
    const candidate = data.candidate || null;

    // R3/S3：无候选时后端把步骤设成 finish，前端要明确「未检验」，不假成功
    if (!candidate || data.noCandidate || (resp && resp.transferStatus === 'no_candidate')) {
      const note = el('div', 'cet-ai-note warn',
        '这次还没有合适的换句材料。已记为「未检验」，你可以继续复习。');
      out.appendChild(note);
      return;
    }

    // R3：选项可能是对象 {id,text} 或纯字符串，必须取文本，不能直接拼对象
    const optionText = (opt, i) => {
      if (typeof opt === 'string') return opt;
      if (opt && typeof opt === 'object') {
        return String(opt.text ?? opt.label ?? opt.id ?? `选项 ${i + 1}`);
      }
      return String(opt ?? '');
    };
    const optionId = (opt, i) => {
      if (opt && typeof opt === 'object' && opt.id != null) return String(opt.id);
      return 'ABCDE'[i] || String(i);
    };

    const box = el('div', 'cet-transfer');
    box.appendChild(el('div', 'cet-transfer-stem',
      String(data.question || '听新片段并作答')));

    const opts = Array.isArray(data.options) ? data.options : [];
    if (!opts.length) {
      box.appendChild(el('div', 'cet-ai-note warn', '候选题没有可用选项，已记为「未检验」。'));
      out.appendChild(box);
      return;
    }

    let submitted = false;
    const buttons = [];
    opts.forEach((opt, i) => {
      const b = el('button', 'cet-opt', `${'ABCDE'[i]}. ${optionText(opt, i)}`);
      b.onclick = async () => {
        if (submitted) return;
        submitted = true;
        buttons.forEach((x) => { x.disabled = true; });
        const ok = await submitTransferAnswer(optionId(opt, i), candidate);
        if (!ok) {
          // R3：提交失败必须能重试，不能显示成功
          submitted = false;
          buttons.forEach((x) => { x.disabled = false; });
        }
      };
      buttons.push(b);
      box.appendChild(b);
    });
    out.appendChild(box);
  }

  // R3：真正提交换句作答（发事件，等后端确认）
  async function submitTransferAnswer(answer, candidate) {
    if (!S.practice || !S.practice.practiceId) return false;
    const out = $('#cet-practice-out');
    try {
      const r = await api('POST', `/practices/${S.practice.practiceId}/events`, {
        eventId: uid('transfer'),
        type: 'transfer_answer',
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
        audioTime: clampToStepRange(media() ? media().currentTime : 0),
        speed: media() ? (media().playbackRate || 1) : 1,
        candidateId: (candidate && (candidate.id || candidate.sentenceId)) || null,
        answer,
      });
      if (out) {
        const res = (r && r.lastTransferResult) || {};
        const correct = res.isCorrect;
        const note = el('div',
          `cet-ai-note ${correct === false ? 'warn' : ''}`,
          correct === true ? '换句作答正确'
            : (correct === false ? '换句作答不正确，已记录'
              : '换句作答已提交（服务端未判定）'));
        out.appendChild(note);
      }
      // S6：换句提交的成功合并也要核对身份
      if (r && S.practice && r.practiceId === S.practice.practiceId) {
        S.practice = { ...S.practice, ...r };
      }
      save();
      return true;
    } catch (err) {
      if (out) {
        out.appendChild(el('div', 'cet-ai-note warn', `换句作答提交失败：${err.message}`));
      }
      return false;
    }
  }

  // 执行本地动作：改播放速度、跳回句子起点、显隐原文
  function executeLocalAction(action, step) {
    const md = media();
    const sid = (S.practice && S.practice.sentenceId) || null;
    const sent = S.sentences.find((s) => s.sentenceId === sid);
    if (!md || !sent) return;

    md.currentTime = sent.startSeconds;
    if (action === 'slow') {
      md.playbackRate = 0.75;
    } else if (action === 'replay') {
      md.playbackRate = 1;
    }
    md.play();

    // 到句末自动停
    const stopAt = sent.endSeconds;
    const guard = () => {
      if (md.currentTime >= stopAt) {
        md.pause();
        md.removeEventListener('timeupdate', guard);
      }
    };
    md.addEventListener('timeupdate', guard);

    if (step && step.showText === false) {
      // 「没听清」先不给原文，保留听力挑战（计划 §5）
      flashHint('先只给声音，试试能不能听出来');
    }
  }

  function showExplainPending() {
    const out = $('#cet-practice-out');
    if (!out) return;
    const note = el('div', 'cet-ai-note', '正在生成解释…（3 秒后仍无结果可继续回听或保存）');
    out.appendChild(note);
    setTimeout(() => {
      if (note.parentNode) {
        note.textContent = '模型暂不可用；你仍可以回听、慢放、看原文或保存本轮。';
        note.classList.add('warn');
      }
    }, 3000);
  }

  async function finishPractice() {
    if (!S.practice) return;
    try {
      const r = await api('POST', `/practices/${S.practice.practiceId}/finish`, {
        feedback: S.practice.latestFeedback || 'sound',
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
      });
      // N3：finish 返回 {practice, reviewItem}，必须合并 r.practice，
      // 不能直接 spread r —— 否则 status 仍是 active，
      // 页面继续显示反馈按钮，再点就得到「练习已结束」。
      const merged = r && r.practice ? r.practice : r;
      S.practice = { ...S.practice, ...(merged || {}) };
      if (r && r.reviewItem) S.reviewItem = r.reviewItem;

      const done = S.practice.status === 'complete';
      flashHint(done ? '本轮已保存，已加入今日复习' : '本轮已保存');
      renderMarks();
      renderPractice();     // 按完成态重绘：停用反馈入口
      save();
    } catch (err) {
      flashHint(`保存失败：${err.message}`, true);
    }
  }

  // ── 播放事件上报（F04）────────────────────────────────
  // 有效用时依赖这些事件：只在前台播放时累计，暂停/后台不累计。
  let playbackStartAt = null;

  // 把时间夹到当前步骤的播放范围内：
  // 后端会校验 PLAYBACK_OUT_OF_RANGE，用户暂停在别处时直接发真实 currentTime 会被拒。
  function clampToStepRange(t) {
    const step = (S.practice && S.practice.currentStep) || {};
    const rng = step.playRange || {};
    const start = typeof rng.startSeconds === 'number' ? rng.startSeconds : null;
    const end = typeof rng.endSeconds === 'number' ? rng.endSeconds : null;
    if (start == null || end == null) return t;
    return Math.min(Math.max(t, start), end);
  }

  function sendEvent(type, extra) {
    if (!S.practice || !S.practice.practiceId) return;
    const payload = {
      eventId: uid(`ev-${type}`),
      type,
      stepId: S.practice.currentStepId,
      materialVersion: S.materialVersion,
      ...extra,
    };
    if (typeof payload.audioTime === 'number') {
      payload.audioTime = Number(clampToStepRange(payload.audioTime).toFixed(3));
    }
    // 事件失败不阻塞界面（计划 §5：落盘失败需显式提示，但不卡操作）
    api('POST', `/practices/${S.practice.practiceId}/events`, payload).catch(() => {});
  }

  function bindPlaybackEvents() {
    const md = media();
    if (!md || md.__cetBound) return;
    md.__cetBound = true;
    md.addEventListener('play', () => {
      playbackStartAt = Date.now();
      sendEvent('playback_start', {
        audioTime: Number((md.currentTime || 0).toFixed(3)),
        speed: md.playbackRate || 1,
      });
    });
    md.addEventListener('pause', () => {
      if (playbackStartAt) {
        sendEvent('playback_end', {
          audioTime: Number((md.currentTime || 0).toFixed(3)),
          speed: md.playbackRate || 1,
          activeMs: Date.now() - playbackStartAt,
        });
        playbackStartAt = null;
      }
    });
    md.addEventListener('ended', () => {
      if (playbackStartAt) {
        sendEvent('playback_end', {
          audioTime: Number((md.currentTime || 0).toFixed(3)),
          speed: md.playbackRate || 1,
          activeMs: Date.now() - playbackStartAt,
        });
        playbackStartAt = null;
      }
    });
    // 页面切到后台时报中断，避免后台时间被算作有效练习
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (playbackStartAt) {
          sendEvent('playback_interrupt', {
          audioTime: Number((md.currentTime || 0).toFixed(3)),
          speed: md.playbackRate || 1,
          reason: 'hidden',
        });
          playbackStartAt = null;
        }
        sendEvent('visibility', { visible: false });
      } else {
        sendEvent('visibility', { visible: true });
      }
    });
  }

  // 统一管理回放守卫：切条目/切动作时先停掉旧的，避免双重播放（计划 T16）
  let activeReplayStop = null;
  function stopActiveReplay() {
    if (activeReplayStop) {
      try { activeReplayStop(); } catch (_) { /* 忽略 */ }
      activeReplayStop = null;
    }
  }
  function replayRange(startSec, endSec) {
    const md = media();
    if (!md || typeof startSec !== 'number') return;
    stopActiveReplay();
    md.currentTime = startSec;
    md.play();
    const onTime = () => {
      if (typeof endSec === 'number' && md.currentTime >= endSec) {
        md.pause();
        md.removeEventListener('timeupdate', onTime);
        if (activeReplayStop === stop) activeReplayStop = null;
      }
    };
    const stop = () => {
      md.removeEventListener('timeupdate', onTime);
      md.pause();
      if (activeReplayStop === stop) activeReplayStop = null;
    };
    activeReplayStop = stop;
    md.addEventListener('timeupdate', onTime);
  }

  // ── 复习（M3）──────────────────────────────────────────
  let reviewState = null;

  function mountReviewPanel() {
    const side = $('.side-column');
    if (!side || $('#cet-review')) return;
    const wrap = el('section', 'cet-panel');
    wrap.id = 'cet-review';
    wrap.hidden = true;
    wrap.innerHTML = `
      <div class="cet-panel-head">
        <strong>今日复习</strong>
        <span id="cet-review-state" class="cet-panel-state">未开始</span>
      </div>
      <div id="cet-review-body"></div>
    `;
    side.insertBefore(wrap, $('#cet-questions'));
  }

  async function startReview() {
    try {
      let rv = await api('POST', '/reviews', { maxItems: 3 });
      // 新建的复习是 ready 状态，skip/complete 需要 active；
      // 先 start 一次（幂等，已在 active 时不报错）
      if (rv.status === 'ready') {
        try {
          rv = await api('POST', `/reviews/${rv.id}/actions`,
            { action: 'start', actionId: uid('rv-start') });
        } catch (_) { /* 已 active 时忽略 */ }
      }
      reviewState = rv;
      renderReview();
      save();
      flashHint('复习已开始（最多三处）');
    } catch (err) {
      if (err.code === 'NO_REVIEW_ITEMS') {
        flashHint('暂时没有待复习的卡点', true);
      } else {
        flashHint(`开始复习失败：${err.message}`, true);
      }
    }
  }

  function renderReview() {
    const box = $('#cet-review');
    const body = $('#cet-review-body');
    const state = $('#cet-review-state');
    if (!box || !body || !reviewState) return;

    box.hidden = false;
    if (state) {
      const mins = Math.round((reviewState.remainingMs || 0) / 60000);
      const phase = reviewState.status === 'paused' ? '已暂停'
        : (reviewState.status === 'complete' ? '已结束' : '进行中');
      state.textContent = `${phase} · 剩余约 ${mins} 分钟 · 第 ${(reviewState.currentPosition || 0) + 1} 处`;
    }

    body.innerHTML = '';
    const entries = reviewState.entries || [];

    // 当前条目
    const cur = entries.find((e) => e.status === 'current') || entries[0];
    if (cur) {
      const card = el('div', 'cet-review-card');
      const sent = (cur.item && cur.item.sentence) || {};
      card.appendChild(el('div', 'cet-review-sentence', sent.text || '(无句子)'));

      const row = el('div', 'cet-review-actions');

      const play = el('button', 'cet-btn-sm', '回听');
      play.onclick = () => {
        // R4：复习条目可能属于别的材料。当前播放器只有本页音源，
        // 跨材料时不能播错，必须明确告知而不是假装回听成功。
        const entryMaterial = (cur.item && cur.item.materialId)
          || (sent && sent.materialId) || null;
        if (entryMaterial && S.materialId && entryMaterial !== S.materialId
            && entryMaterial !== S.materialId.replace(/^cet[46]-/, '')) {
          flashHint('该复习条目属于其他材料，请先打开对应课程再回听', true);
          return;
        }
        // 字段名是 startSeconds/endSeconds；起点允许为 0。
        replayRange(sent.startSeconds, sent.endSeconds);
      };
      row.appendChild(play);

      const skip = el('button', 'cet-btn-sm', '跳过');
      skip.onclick = () => reviewAction('skip', cur.practiceId);
      row.appendChild(skip);

      const done = el('button', 'cet-btn-sm', '这处练完了');
      done.onclick = () => reviewAction('complete', cur.practiceId);
      row.appendChild(done);

      card.appendChild(row);
      body.appendChild(card);
    } else {
      body.appendChild(el('div', 'cet-empty', '本次复习没有条目'));
    }

    // 全部条目进度
    const list = el('div', 'cet-review-list');
    entries.forEach((e, i) => {
      const label = { current: '进行中', pending: '待复习', done: '已完成', skipped: '已跳过' }[e.status] || e.status;
      const row = el('div', `cet-review-row cet-review-${e.status}`);
      row.appendChild(el('span', 'cet-review-idx', `${i + 1}`));
      row.appendChild(el('span', 'cet-review-label', label));
      const t = (e.item && e.item.sentence && e.item.sentence.text) || '';
      row.appendChild(el('span', 'cet-review-text', t.slice(0, 40)));
      list.appendChild(row);
    });
    body.appendChild(list);

    // 控制
    // 控制：按后端真实状态决定给「暂停」还是「继续」（F02）
    const ctrl = el('div', 'cet-review-ctrl');
    if (reviewState.status === 'paused') {
      const resume = el('button', 'cet-btn-sm', '继续复习');
      resume.onclick = () => reviewAction('resume');
      ctrl.appendChild(resume);
    } else if (reviewState.status === 'active') {
      const pause = el('button', 'cet-btn-sm', '暂停');
      pause.onclick = () => reviewAction('pause');
      ctrl.appendChild(pause);
    }

    const end = el('button', 'cet-btn-sm', '结束复习');
    end.onclick = () => reviewAction('finish');
    ctrl.appendChild(end);
    body.appendChild(ctrl);
  }

  async function reviewAction(action, practiceId) {
    if (!reviewState) return;
    // R4：暂停 / 跳过 / 结束都要先停掉正在播的回放，
    // 否则页面显示「已暂停」但音频还在响。
    if (action === 'pause' || action === 'skip' || action === 'complete' || action === 'finish') {
      stopActiveReplay();
    }
    try {
      // 后端要求 actionId 用于幂等去重（重复提交不重复计时）
      const payload = { action, actionId: uid(`rv-${action}`) };
      if (practiceId) payload.practiceId = practiceId;
      const r = await api('POST', `/reviews/${reviewState.id}/actions`, payload);
      reviewState = { ...reviewState, ...r };
      renderReview();
      save();
    } catch (err) {
      if (err.code === 'STALE_RESPONSE') {
        flashHint('复习状态已变化，请刷新', true);
      } else {
        flashHint(`复习操作失败：${err.message}`, true);
      }
    }
  }

  // ── 恢复 ────────────────────────────────────────────────
  // R1：按材料 ID 分键保存，避免在四级页面恢复出六级的成绩与练习。
  const stateKey = (mid) => `cet.state.v2.${mid || 'unknown'}`;

  function save() {
    if (!S.materialId) return;
    try {
      localStorage.setItem(stateKey(S.materialId), JSON.stringify({
        materialId: S.materialId,
        attemptId: S.attempt && S.attempt.attemptId,
        practiceId: S.practice && S.practice.practiceId,
        reviewId: reviewState && reviewState.id,
      }));
    } catch (_) { /* 隐私模式可能失败，不阻塞 */ }
  }

  function loadSaved() {
    if (!S.materialId) return {};
    try {
      const raw = localStorage.getItem(stateKey(S.materialId));
      if (!raw) return {};
      const saved = JSON.parse(raw);
      // 二次核对：键里存的材料必须与当前材料一致，否则丢弃
      if (saved.materialId && saved.materialId !== S.materialId) return {};
      return saved;
    } catch (_) { return {}; }
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

    // F03：恢复交卷结果（原来只恢复标记，刷新后右侧回到「未交卷」）
    try {
      const saved = loadSaved();
      if (saved.attemptId) {
        const at = await api('GET', `/attempts/${saved.attemptId}`);
        // R1：归属核对——取回的记录必须属于当前材料，否则丢弃（防跨材料串入）
        if (at && at.attemptId && at.materialId === S.materialId) {
          S.attempt = at;
          renderAttemptResult(at);
          const st = $('#cet-attempt-state');
          if (st) { st.textContent = `已交卷 · 得分 ${at.score}`; st.classList.add('done'); }
        }
      }
    } catch (_) { /* 交卷恢复失败不阻塞 */ }

    // F03：恢复带练（原来只恢复标记，刷新后带练面板消失）
    try {
      const saved = loadSaved();
      if (saved.practiceId) {
        const pr = await api('GET', `/practices/${saved.practiceId}`);
        // R1：练习也必须属于当前材料
        if (pr && pr.practiceId && pr.materialId === S.materialId) {
          S.practice = pr;
          renderPractice();
          renderMarks();
          bindPlaybackEvents();
          flashHint('已恢复上次带练');
        }
      }
    } catch (_) { /* 带练恢复失败不阻塞 */ }

    // 恢复复习会话（计划 T11：重启后恢复原会话和步骤）
    try {
      const saved = loadSaved();
      if (saved.reviewId) {
        const rv = await api('GET', `/reviews/${saved.reviewId}`);
        if (rv && rv.status && rv.status !== 'complete') {
          reviewState = rv;
          mountReviewPanel();
          renderReview();
          flashHint('已恢复上次复习');
        }
      }
    } catch (_) { /* 复习恢复失败不阻塞 */ }
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
  // 导出给自动化测试用（R6：测试曾读不存在的 __reviewState 导致断言被跳过）
  window.__cet = {
    S, api, setListening, createMark, confirmMark, submitAttempt,
    getReviewState: () => reviewState,
  };
})();
