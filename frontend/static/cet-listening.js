/* 四六级卡点训练 · 前端模块
 *
 * 设计原则（按施工计划 §1、§4）：
 *   - 只补新增功能必需的控件，不重做 UI、不改原导航与样式
 *   - 独立文件挂载，尽量少改 workspace.html（5080 行）
 *   - **M** 键标记「没听清」的位置（返修 2.1 起从空格改为 M）；Space 归底座做播放/暂停；
 *     输入框/按钮/弹窗里的按键不抢
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
    // F08：这个值现在**真的发给后端**（reactionDelaySeconds）。
    // 后端 create_mark() 用它算 adjusted = pressedAtSec - lookbackSec * speed，
    // 再用 adjusted 去吸附候选句 —— 所以改这里就能改变候选定位。
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
    // F08：预览目标（markId → sentenceId）。预览只切试听、不落库；
    // 只有点「确认这句」才 PATCH /marks/{id} 写 confirmedSentenceId。
    previews: {},
    // F05：换句闭环状态
    transferPlayed: false,   // 候选原声是否已完整播过（后端要求先有 playedToEnd）
    transferDone: false,     // 本轮换句是否已作答
    transferCandidate: null, // 当前候选（含跨材料音频地址）
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
    // 关键：点完按钮后焦点会留在按钮上，之后按键会被浏览器当成「激活按钮」，
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
        ? '播放时按 M 标记「没听清」的位置（Space 是暂停/继续）'
        : '';
    }
    document.body.classList.toggle('cet-listening', on);

    // F05：进入卡点练习模式即隐藏原文（字幕 + 含原文的内容大纲 + 阅读正文）。
    // 只控显隐，原 UI 完整保留 —— 退出时恢复。
    if (on) {
      applyTextVisibility(false);
      ensureTimeline();
      renderTimelineMarks();
    } else {
      applyTextVisibility(true);
      // 退出练习模式时清掉换句区的临时状态
      S.transferPlayed = false;
    }
  }

  // ── M 键标记（返修 2.1：原为空格）──────────────────────────
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
    // 返修 2.1：卡点标记键从 Space 改为 **M**。
    //
    // 为什么改：Space 在听力训练里是最高频的「暂停/继续」，底座 cet-player.js
    // 也把它当播放开关。两边都想要 Space 时，无论谁让出都会出问题 ——
    // 实测（队友挂载但未进练习模式）Space 彻底失效：底座认为队友接管了、
    // 队友 onKeyDown 又因 !S.listening 直接 return，两边都没处理。
    // 现在 Space 归底座独占（暂停/播放），标记用 M（Mnemonic: Mark）。
    if (e.code !== 'KeyM') return;
    if (!S.listening) return;
    if (isTypingTarget(e.target)) return;
    // 按钮聚焦时按键是「激活」，不抢 —— 但练习模式下用户可能刚点过按钮，
    // 此时按钮仍在聚焦状态。已在 setListening 里 blur，这里再兜一层：
    // 只要不是 cet 自己的按钮，就不抢。
    const onButton = (e.target.tagName || '').toLowerCase() === 'button';
    const isCetButton = onButton && e.target.closest && e.target.closest('.cet-btn, .cet-btn-sm');
    if (onButton && !isCetButton) return;
    if (e.repeat) return;                  // 忽略长按连发

    const m = media();
    if (!m) return;

    e.preventDefault();
    // 防连发：300ms 内只认一次
    const now = Date.now();
    if (now - S.lastMarkAt < 300) return;
    S.lastMarkAt = now;

    createMark(m.currentTime);
  }

  async function createMark(pressedAtSec) {
    if (!S.materialId) return;
    // F08：创建标记必须带上**回溯参数**与**实际倍速**。
    // 后端 create_mark() 用 adjusted = pressedAtSec - reactionDelaySeconds * speed
    // 去吸附候选句，不传这两个值就用后端默认值（0.6 / 1.0），
    // 前端 CFG.lookbackSec = 2.5 就永远不生效。
    const md = media();
    const speed = md && md.playbackRate ? md.playbackRate : 1;
    try {
      const mark = await api('POST', '/marks', {
        materialId: S.materialId,
        materialVersion: S.materialVersion,
        listeningSessionId: sessionId(),
        pressedAtSec: Number(pressedAtSec.toFixed(3)),
        reactionDelaySeconds: CFG.lookbackSec,     // 回溯窗口（秒）
        speed: Number(speed.toFixed(3)),           // 实际播放倍速
      });
      S.marks.push(mark);
      renderMarks();
      // F08：标记一建立就自动进入「候选预览」——立刻回听候选句，
      // 用户听完再决定要不要确认，不需要先手动点「回听」。
      previewMark(mark, mark.suggestedSentenceId, { autoplay: true });
      flashHint(`已在 ${fmt(mark.pressedAtSec)} 标记（回看 ${CFG.lookbackSec}s / ${speed}x）`);
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
      hint.textContent = S.listening ? '播放时按 M 标记「没听清」的位置（Space 是暂停/继续）' : '';
      hint.classList.remove('error');
    }, 2600);
  }

  // ── 标记渲染（三态：待确认 / 待练 / 本轮已练）──────────
  function markState(m) {
    // F08：「改一下」把已确认的标记退回待确认态，允许重新预览与覆盖。
    if (m.__amending) return 'pending';
    if (m.confirmedSentenceId) {
      return S.practice && S.practice.markId === m.markId && S.practice.status === 'complete'
        ? 'done' : 'ready';
    }
    return 'pending';
  }

  // F08：当前预览的句子 = 用户切过的目标句，否则是后端建议句。
  // 注意预览**不改 mark 本身**，只有确认才写 confirmedSentenceId。
  function previewSentenceId(m) {
    return S.previews[m.markId] || m.suggestedSentenceId || null;
  }

  function findSentence(sentenceId) {
    return S.sentences.find((s) => s.sentenceId === sentenceId) || null;
  }

  function renderMarks() {
    const box = $('#cet-marks');
    if (!box) return;
    box.innerHTML = '';
    if (!S.marks.length) {
      box.hidden = true;
      renderTimelineMarks();
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

      // F08：待确认时显示「预览中的句子」，并明确标出它还不是最终确认句
      const shownId = st === 'pending' ? previewSentenceId(m) : m.confirmedSentenceId;
      const sent = findSentence(shownId);
      if (sent) {
        const q = el('span', 'cet-mark-sentence', sent.text);
        row.appendChild(q);
        if (st === 'pending') {
          const tag = el('span', 'cet-mark-preview-tag',
            `预览 ${fmt(sent.startSeconds)}–${fmt(sent.endSeconds)}`);
          row.appendChild(tag);
        }
      }

      const acts = el('span', 'cet-mark-actions');

      // 回听：待确认时回听「预览句」，已确认时回听确认句
      const play = el('button', 'cet-btn-sm', '回听');
      play.onclick = () => previewMark(m, shownId, { autoplay: true });
      acts.appendChild(play);

      // F08：待确认 —— 前后句只切预览，不确认
      if (st === 'pending') {
        const prev = el('button', 'cet-btn-sm', '前一句');
        prev.onclick = () => adjustMark(m, -1);
        acts.appendChild(prev);

        const next = el('button', 'cet-btn-sm', '后一句');
        next.onclick = () => adjustMark(m, 1);
        acts.appendChild(next);

        // 只有这里才真的落库确认
        const conf = el('button', 'cet-btn-sm cet-btn-confirm', '确认这句');
        conf.onclick = () => confirmMark(m, previewSentenceId(m));
        acts.appendChild(conf);
      }

      if (st === 'ready') {
        const start = el('button', 'cet-btn-sm', '开始带练');
        start.onclick = () => startPractice(m);
        acts.appendChild(start);

        // F08：确认后仍能纠正 —— 回到待确认状态重新预览、重新确认
        const amend = el('button', 'cet-btn-sm', '改一下');
        amend.onclick = () => reopenMark(m);
        acts.appendChild(amend);
      }

      // 删除标记：已开始带练的标记后端会拒（MARK_IN_PRACTICE），前端给明确提示。
      const del = el('button', 'cet-btn-sm cet-btn-danger', '删除');
      del.onclick = () => deleteMark(m);
      acts.appendChild(del);

      row.appendChild(acts);
      box.appendChild(row);
    });

    renderTimelineMarks();
  }

  // F08：预览 = 只播该句 + 记录预览目标，**不发 PATCH**
  function previewMark(m, sentenceId, opts) {
    const sid = sentenceId || previewSentenceId(m);
    const sent = findSentence(sid);
    if (!sent) return;
    S.previews[m.markId] = sid;
    // 预览会改变「当前预览句」，列表里的句子文本与预览标签要跟着更新
    renderMarks();
    if (opts && opts.autoplay) {
      replayRange(sent.startSeconds, sent.endSeconds);
    }
  }

  // F08：前后句只切换预览，并且**立即回听**该句（用户听完再决定）
  function adjustMark(m, delta) {
    const cur = previewSentenceId(m);
    let idx = S.sentences.findIndex((s) => s.sentenceId === cur);
    if (idx < 0) idx = 0;
    idx = Math.min(S.sentences.length - 1, Math.max(0, idx + delta));
    const target = S.sentences[idx];
    if (!target) return;
    previewMark(m, target.sentenceId, { autoplay: true });
    flashHint(`预览第 ${idx + 1} 句，听完再点「确认这句」`);
  }

  async function confirmMark(m, sentenceId) {
    const sid = sentenceId || previewSentenceId(m);
    if (!sid) { flashHint('没有可确认的句子', true); return; }
    try {
      const updated = await api('PATCH', `/marks/${m.markId}`, { confirmedSentenceId: sid });
      Object.assign(m, updated);
      // 确认后清掉预览态：后续「回听」应播确认句
      delete S.previews[m.markId];
      delete m.__amending;
      renderMarks();
      flashHint('已确认卡点句子（还可以点「改一下」重新调整）');
    } catch (err) {
      if (err.code === 'ATTEMPT_REQUIRED') {
        flashHint('请先交卷，再确认卡点句子', true);
      } else {
        flashHint(`确认失败：${err.message}`, true);
      }
    }
  }

  async function deleteMark(m) {
    try {
      await api('DELETE', `/marks/${encodeURIComponent(m.markId)}`);
      S.marks = S.marks.filter((x) => x.markId !== m.markId);
      renderMarks();
      flashHint('已删除该标记');
    } catch (err) {
      if (err.code === 'MARK_IN_PRACTICE') {
        flashHint('该标记已开始带练，不能删除', true);
      } else {
        flashHint(`删除失败：${err.message}`, true);
      }
    }
  }

  // F08：确认后仍能调整 —— 把预览起点拉回已确认句，重新进入待确认态。
  // 不直接清空后端 confirmedSentenceId（那会让已建立的带练失去依据），
  // 而是让用户重新预览并再次确认，覆盖原值。
  function reopenMark(m) {
    S.previews[m.markId] = m.confirmedSentenceId || m.suggestedSentenceId;
    // 前端视角退回「待确认」：用一个本地标记区分「已确认但正在改」
    m.__amending = true;
    renderMarks();
    const sent = findSentence(previewSentenceId(m));
    if (sent) replayRange(sent.startSeconds, sent.endSeconds);
    flashHint('重新调整：用「前一句 / 后一句」试听，确认后覆盖原句');
  }

  // ── F10：时间线颜色标记 ─────────────────────────────────
  // 在播放器下方画一条与音频时长等宽的时间轴，把每个标记画在它的真实秒数位置。
  // 三态：橙「待确认」/ 蓝「待练」/ 绿「本轮已练」。
  // 无障碍：颜色 + 文字双重标识 —— 每个标记块里有状态文字，
  // 并有 title / aria-label，色盲用户不依赖颜色也能分辨。
  const TIMELINE_STATE = {
    pending: { label: '待确认', short: '待' },
    ready: { label: '待练', short: '练' },
    done: { label: '本轮已练', short: '已' },
  };

  function timelineDuration() {
    const md = media();
    if (md && isFinite(md.duration) && md.duration > 0) return md.duration;
    const last = S.sentences[S.sentences.length - 1];
    return last && isFinite(last.endSeconds) ? last.endSeconds : 0;
  }

  function ensureTimeline() {
    let layer = $('#cet-timeline');
    if (layer) return layer;
    const md = media();
    if (!md) return null;
    // 注意：不能挂在 #local-media-player 的父节点（也就是 #youtube-player）里 ——
    // initLocalMedia() 会整块重写 #youtube-player 的 innerHTML，
    // 实测时间线刚挂上就被清掉（MutationObserver 抓到 removed）。
    // 改用稳定的 .video-card（只切 hidden，不重写子节点），
    // 把时间线作为 #youtube-player 的**兄弟节点**放在播放器下方。
    const host = md.closest('.video-card') || md.parentNode;
    if (!host) return null;
    const wrap = el('div', 'cet-timeline');
    wrap.id = 'cet-timeline';
    wrap.setAttribute('data-cet-mark-layer', '1');
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', '卡点标记时间线');

    const track = el('div', 'cet-timeline-track');
    track.id = 'cet-timeline-track';
    wrap.appendChild(track);

    const legend = el('div', 'cet-timeline-legend');
    [['pending', '待确认'], ['ready', '待练'], ['done', '本轮已练']].forEach(([st, label]) => {
      const item = el('span', `cet-timeline-legend-item cet-timeline-legend-${st}`);
      const dot = el('span', `cet-timeline-dot cet-timeline-dot-${st}`);
      dot.setAttribute('aria-hidden', 'true');
      item.appendChild(dot);
      item.appendChild(el('span', null, label));
      legend.appendChild(item);
    });
    wrap.appendChild(legend);

    // 插到播放器之后；若播放器直接是 .video-card 的子节点
    const player = md.closest('#youtube-player') || md;
    if (player.parentNode === host) {
      host.insertBefore(wrap, player.nextSibling);
    } else {
      host.appendChild(wrap);
    }
    return wrap;
  }

  function renderTimelineMarks() {
    const wrap = ensureTimeline();
    if (!wrap) return;
    const track = $('#cet-timeline-track');
    if (!track) return;
    const dur = timelineDuration();
    track.innerHTML = '';
    if (!dur) return;

    track.setAttribute('data-cet-duration-sec', String(Number(dur.toFixed(3))));

    if (!S.marks.length) {
      track.appendChild(el('div', 'cet-timeline-empty', '暂无卡点标记（播放时按 M 标记）'));
      return;
    }

    S.marks.forEach((m) => {
      const st = markState(m);
      const sec = Number(m.pressedAtSec) || 0;
      const pct = Math.min(100, Math.max(0, (sec / dur) * 100));
      const meta = TIMELINE_STATE[st];

      const tick = el('button', `cet-timeline-mark cet-timeline-mark-${st}`);
      tick.type = 'button';
      tick.style.left = `${pct}%`;
      // 无障碍 + 可测性：秒数与状态都写在属性里，不依赖颜色
      tick.setAttribute('data-cet-mark', '1');
      tick.setAttribute('data-cet-mark-sec', String(Number(sec.toFixed(3))));
      tick.setAttribute('data-cet-mark-state', st);
      tick.setAttribute('title', `${fmt(sec)} · ${meta.label}`);
      tick.setAttribute('aria-label', `卡点 ${fmt(sec)}，状态：${meta.label}`);
      tick.appendChild(el('span', 'cet-timeline-mark-text', meta.short));
      tick.onclick = () => {
        // 点时间线标记 = 跳到该位置并进入预览/回听
        const sentId = st === 'pending' ? previewSentenceId(m) : m.confirmedSentenceId;
        const sent = findSentence(sentId);
        const md = media();
        if (sent) {
          replayRange(sent.startSeconds, sent.endSeconds);
        } else if (md) {
          md.currentTime = sec;
        }
      };
      track.appendChild(tick);
    });
  }


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
      // F05：恢复带练时若停在换句步骤，同样要隐藏原文并恢复原速
      hideTextForTransfer();
      renderTransfer({ currentStep: step, transferStatus: S.practice.transferStatus }, container);
      return;
    }

    if (action === 'slow' || action === 'replay' || action === 'chunk') {
      // F05：恢复步骤时按后端的 textVisible 决定字幕显隐
      applyTextVisibility(step.showText !== false && step.textVisible !== false);
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

  // B01：单调状态合并。返回 null 表示回包比当前状态旧，应丢弃。
  // 规则：同一练习 + revision 不得回退 + 已完成态不可退回进行中。
  function mergePracticeState(current, incoming) {
    if (!current) return incoming || null;
    if (!incoming) return null;
    if (incoming.practiceId && current.practiceId
        && incoming.practiceId !== current.practiceId) {
      return null;                                  // 跨练习，丢弃
    }
    const curRev = typeof current.revision === 'number' ? current.revision : -1;
    const inRev = typeof incoming.revision === 'number' ? incoming.revision : -1;
    if (inRev >= 0 && curRev >= 0 && inRev < curRev) {
      return null;                                  // 旧状态，丢弃
    }
    // 已完成不能被旧回包退回进行中
    if (current.status === 'complete' && incoming.status && incoming.status !== 'complete') {
      return null;
    }
    return {
      ...current,
      ...incoming,
      currentStepId: incoming.currentStepId || current.currentStepId,
    };
  }

  // N2：冲突后从服务端拉取权威状态，让用户能直接继续
  async function resyncPractice() {
    if (!S.practice || !S.practice.practiceId) return;
    try {
      const wantId = S.practice.practiceId;
      const pr = await api('GET', `/practices/${wantId}`);
      // B01：GET 回包也要核对身份与 revision，防止切换练习后旧查询覆盖新状态
      if (pr && pr.practiceId === wantId) {
        const merged = mergePracticeState(S.practice, pr);
        if (merged === null) return;
        S.practice = merged;
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

      // B01：身份过滤 + **单调合并**。
      // 一个成功响应只代表它生成时的服务状态，不保证到达时仍最新；
      // 原来无条件 {...S.practice, ...r} 会让晚到的旧回包把新状态覆盖回去
      //（实测：已推进到 explain 后，早先的 slow 回包到达，内存退回旧步骤，
      //  接着点保存就 409）。
      if (!S.practice || S.practice.practiceId !== practiceAtSend) return;

      // 单调合并：只接受 revision 不小于当前的状态，绝不回退
      const merged = mergePracticeState(S.practice, r);
      if (merged === null) {
        // 回包比当前状态旧：只同步「不影响步骤」的字段，不动 currentStepId/status
        return;
      }
      S.practice = merged;
      save();

      // 只有最新一次请求才负责更新界面文案，避免旧回包闪回旧动作
      if (seq !== feedbackSeq) return;

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
        // F05：换句是「独立检验」——必须隐藏原文（含大纲摘要），
        // 并恢复 1.0x 原速，否则候选听力被字幕和慢放辅助，检验无效。
        hideTextForTransfer();
        renderTransfer(r);
      } else {
        // 非换句步骤：按后端 textVisible 决定是否隐藏原文
        applyTextVisibility(step.showText !== false && step.textVisible !== false);
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

  // F05：换句检验闭环。
  // 三条硬约束（都来自后端真实校验，不能靠改文案绕过）：
  //   1) transfer_answer 要求当前步骤先有 playedToEnd=1 的 playback_end
  //   2) playback_start/end 的 speed 必须等于 step.speed，否则 422
  //   3) playback 事件要求 audioTime 落在 step 的 [start-0.25, end+0.7] 内
  // 所以这里真的播候选音频，播完再放行作答。
  // 换句区的状态提示只保留一条：自动播放 + 手动重播会各发一次，
  // 不去重就会堆出「已完整播放」两遍（实测出现过）。
  function transferNote(text, warn, keepId) {
    const box = $('#cet-transfer-box');
    const host = box || $('#cet-practice-out');
    if (!host) return null;
    const key = keepId || 'transfer-status';
    const old = host.querySelector(`[data-cet-note="${key}"]`);
    if (old) old.remove();
    const note = el('div', `cet-ai-note${warn ? ' warn' : ''}`, text);
    note.setAttribute('data-cet-note', key);
    host.appendChild(note);
    return note;
  }

  // 候选原声属于**另一份材料/音源**，不能复用页面播放器（会打断原句播放、
  // 且 src 不同）。用一个独立 audio 元素播真实音频。
  function candidateAudio() {
    let a = document.getElementById('cet-transfer-audio');
    if (!a) {
      a = document.createElement('audio');
      a.id = 'cet-transfer-audio';
      a.preload = 'auto';
      a.setAttribute('data-cet-transfer-audio', '1');
      a.style.display = 'none';
      document.body.appendChild(a);
    }
    return a;
  }

  function waitMetadata(a) {
    if (a.readyState >= 1 && isFinite(a.duration)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const ok = () => { cleanup(); resolve(); };
      const bad = () => { cleanup(); reject(new Error('候选音频加载失败')); };
      const cleanup = () => {
        a.removeEventListener('loadedmetadata', ok);
        a.removeEventListener('error', bad);
      };
      a.addEventListener('loadedmetadata', ok, { once: true });
      a.addEventListener('error', bad, { once: true });
      setTimeout(bad, 8000);
    });
  }

  // F05：恢复 1.0x 原速（页面播放器 + 原倍速下拉 + 候选播放器一起恢复，
  // 否则「界面上显示 1x、实际还在 0.75」这种不一致会骗过用户）
  function restoreOriginalSpeed() {
    const md = media();
    if (md) md.playbackRate = 1;
    const sel = document.getElementById('playback-rate');
    if (sel && sel.value !== '1') {
      sel.value = '1';
      if (typeof window.onPlaybackRateChange === 'function') {
        try { window.onPlaybackRateChange(); } catch (_) { /* 原页面异常不阻塞 */ }
      }
    }
    const ca = document.getElementById('cet-transfer-audio');
    if (ca) ca.playbackRate = 1;
  }

  // F05：播放候选的真实音频，并发出满足后端 playedToEnd 判定的播放事件。
  async function playTransferCandidate(candidate) {
    if (!S.practice || !S.practice.practiceId) return false;
    const step = (S.practice.currentStep || {});
    const speed = typeof step.speed === 'number' ? step.speed : 1;
    const start = Number(candidate.startSeconds);
    const end = Number(candidate.endSeconds);
    const a = candidateAudio();

    if (!candidate.audioUrl || !isFinite(start) || !isFinite(end) || end <= start) {
      transferNote('候选音频地址或时间范围无效，本次记为「未检验」，不作为通过。', true);
      return false;
    }

    restoreOriginalSpeed();                 // 换句必须原速听
    S.transferPlayed = false;

    const btn = $('#cet-transfer-play');
    if (btn) btn.disabled = true;
    const playing = transferNote('正在播放候选原声（真实音频，非原句）…', false, 'transfer-play');

    try {
      a.src = candidate.audioUrl;
      await waitMetadata(a);
      a.playbackRate = speed;               // 必须等于 step.speed，否则后端 422
      a.currentTime = start;
      await a.play();
    } catch (err) {
      if (playing && playing.parentNode) playing.remove();
      transferNote(`候选音频无法播放：${err.message}。本次记为「未检验」。`, true);
      if (btn) btn.disabled = false;
      return false;
    }

    // 1) 播放开始：audioTime 必须 ≤ step.start + 0.25
    await postPracticeEvent('playback_start', {
      audioTime: start,
      speed,
    });

    // 2) 播到候选句终点
    const ok = await new Promise((resolve) => {
      let done = false;
      const finish = (value) => {
        if (done) return;
        done = true;
        a.removeEventListener('timeupdate', onTime);
        a.removeEventListener('ended', onEnd);
        resolve(value);
      };
      const onTime = () => {
        if (a.currentTime >= end - 0.05) { a.pause(); finish(true); }
      };
      const onEnd = () => finish(true);
      a.addEventListener('timeupdate', onTime);
      a.addEventListener('ended', onEnd);
      // 兜底：超过真实时长 2 倍还没结束就放弃（不伪造 activeMs）
      setTimeout(() => finish(a.currentTime >= end - 0.5), Math.max(3000, (end - start) * 2000 / Math.max(speed, 0.1) + 3000));
    });

    // 3) 播放结束：audioTime 要落在 step.end 附近，activeMs 要接近理论时长
    const realMs = Math.round(((end - start) / Math.max(speed, 0.1)) * 1000);
    const evResp = await postPracticeEvent('playback_end', {
      audioTime: end,
      speed,
      activeMs: realMs,
    });

    const applied = !!(evResp && evResp.event && evResp.event.playedToEnd);
    S.transferPlayed = applied;

    if (playing && playing.parentNode) playing.remove();
    if (btn) btn.disabled = false;

    if (ok && applied) {
      transferNote('候选原声已完整播放，可以作答。');
      setTransferOptionsEnabled(true);
      return true;
    }
    transferNote('没有完整播完（可能被暂停或跳过），请点「重播候选」听完再作答。', true);
    setTransferOptionsEnabled(false);
    return false;
  }

  // 统一的练习事件上报（返回响应，供判定 playedToEnd）
  async function postPracticeEvent(type, extra) {
    if (!S.practice || !S.practice.practiceId) return null;
    try {
      return await api('POST', `/practices/${S.practice.practiceId}/events`, {
        eventId: uid(`ev-${type}`),
        type,
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
        ...extra,
      });
    } catch (err) {
      transferNote(`播放事件上报失败：${err.message}`, true);
      return null;
    }
  }

  function setTransferOptionsEnabled(on) {
    document.querySelectorAll('#cet-transfer-box .cet-opt').forEach((b) => {
      if (b.dataset.cetAnswered === '1') return;
      b.disabled = !on;
    });
  }

  // F05：换句检验渲染（有候选：真实音频 + 题面 + 作答；无候选：明确未检验）
  function renderTransfer(resp, container) {
    const out = container || $('#cet-practice-out');
    if (!out) return;
    const step = (resp && resp.currentStep) || (S.practice && S.practice.currentStep) || {};
    const data = step.data || {};
    const candidate = data.candidate || null;
    const noCandidate = !candidate || data.noCandidate === true
      || (resp && resp.transferStatus === 'no_candidate')
      || (S.practice && S.practice.transferStatus === 'no_candidate');

    // 每次重绘前清掉旧的换句区，避免重复挂载
    const old = $('#cet-transfer-box');
    if (old) old.remove();

    const box = el('div', 'cet-transfer');
    box.id = 'cet-transfer-box';
    box.setAttribute('data-cet-transfer', noCandidate ? 'no_candidate' : 'presented');

    // 无候选：明确「未检验」，不发任何事件、不当作通过
    if (noCandidate) {
      box.appendChild(el('div', 'cet-transfer-stem', '换句检验'));
      const note = el('div', 'cet-ai-note warn',
        '这次没有合适的换句材料，本轮记为「未检验」——不能算作已通过。');
      note.setAttribute('data-cet-unverified', '1');
      box.appendChild(note);
      const tip = el('div', 'cet-ai-note',
        '你仍可以回听原句、慢放或保存本轮；换句材料补齐后可以再练一次。');
      box.appendChild(tip);
      out.appendChild(box);
      S.transferPlayed = false;
      S.transferCandidate = null;
      return;
    }

    S.transferCandidate = candidate;
    S.transferPlayed = false;

    box.appendChild(el('div', 'cet-transfer-stem', '换句检验（候选来自其他真实音频）'));

    // 候选句本身是听力材料，**不显示原文**（否则等于泄题）
    const meta = el('div', 'cet-transfer-meta',
      `候选音频 ${fmt(candidate.startSeconds)}–${fmt(candidate.endSeconds)}`
      + `${candidate.materialId ? ` · ${candidate.materialId}` : ''}`);
    box.appendChild(meta);

    const playRow = el('div', 'cet-transfer-actions');
    const playBtn = el('button', 'cet-btn-sm', '重播候选');
    playBtn.id = 'cet-transfer-play';
    playBtn.onclick = () => playTransferCandidate(candidate);
    playRow.appendChild(playBtn);
    box.appendChild(playRow);

    const gate = el('div', 'cet-ai-note', '先听完候选原声，选项才会解锁。');
    gate.id = 'cet-transfer-gate';
    box.appendChild(gate);

    // 题面与选项
    const questionText = String(data.question || '').trim();
    if (questionText) {
      box.appendChild(el('div', 'cet-transfer-question', questionText));
    }

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

    const opts = Array.isArray(data.options) ? data.options : [];
    if (!opts.length) {
      box.appendChild(el('div', 'cet-ai-note warn', '候选题没有可用选项，已记为「未检验」。'));
      box.setAttribute('data-cet-transfer', 'no_candidate');
      out.appendChild(box);
      return;
    }

    let submitted = false;
    const buttons = [];
    opts.forEach((opt, i) => {
      const b = el('button', 'cet-opt', `${'ABCDE'[i]}. ${optionText(opt, i)}`);
      b.disabled = true;                     // 播完才解锁
      b.onclick = async () => {
        if (submitted || b.disabled) return;
        submitted = true;
        buttons.forEach((x) => { x.disabled = true; });
        const ok = await submitTransferAnswer(optionId(opt, i), candidate);
        if (!ok) {
          submitted = false;
          buttons.forEach((x) => { x.disabled = false; });
        } else {
          b.dataset.cetAnswered = '1';
        }
      };
      buttons.push(b);
      box.appendChild(b);
    });
    out.appendChild(box);

    // 进入换句即自动播一次候选真实音频（用户不用先找按钮）
    playTransferCandidate(candidate);
  }

  // R3：真正提交换句作答（发事件，等后端确认）
  async function submitTransferAnswer(answer, candidate) {
    if (!S.practice || !S.practice.practiceId) return false;
    const out = $('#cet-practice-out');
    const step = (S.practice.currentStep || {});
    const speed = typeof step.speed === 'number' ? step.speed : 1;
    const rng = step.playback || step.playRange || {};
    const endSec = typeof rng.endSec === 'number' ? rng.endSec
      : (typeof rng.endSeconds === 'number' ? rng.endSeconds : null);
    try {
      const r = await api('POST', `/practices/${S.practice.practiceId}/events`, {
        eventId: uid('transfer'),
        type: 'transfer_answer',
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
        // 作答时音频停在候选句终点，speed 必须与步骤一致
        audioTime: endSec != null ? endSec : clampToStepRange(media() ? media().currentTime : 0),
        speed,
        candidateId: (candidate && (candidate.id || candidate.sentenceId)) || null,
        answer,
      });
      if (out) {
        const res = (r && r.lastTransferResult) || {};
        const correct = res.isCorrect;
        const note = el('div',
          `cet-ai-note ${correct === false ? 'warn' : ''}`,
          correct === true ? '换句作答正确（辅助检验，不等于独立听懂）'
            : (correct === false ? '换句作答不正确，已记录'
              : '换句作答已提交（服务端未判定）'));
        out.appendChild(note);
      }
      // S6：换句提交的成功合并也要核对身份
      if (r && S.practice && r.practiceId === S.practice.practiceId) {
        S.practice = { ...S.practice, ...r };
      }
      setTransferOptionsEnabled(false);
      save();
      return true;
    } catch (err) {
      if (out) {
        const hint = err.code === 'PLAYBACK_REQUIRED'
          ? '请先完整播放候选原声（点「重播候选」听完）再作答。'
          : `换句作答提交失败：${err.message}`;
        out.appendChild(el('div', 'cet-ai-note warn', hint));
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

    // F05：原速恢复统一走 restoreOriginalSpeed，保证「播放器 + 下拉框」一致
    if (action === 'slow') {
      md.playbackRate = 0.75;
      const sel = document.getElementById('playback-rate');
      if (sel) sel.value = '0.75';
    } else if (action === 'replay') {
      restoreOriginalSpeed();
    }

    md.currentTime = sent.startSeconds;
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

    // F05：showText=false 时必须真的隐藏原文，不能只弹一句提示
    applyTextVisibility(step ? step.showText !== false && step.textVisible !== false : true);

    if (step && step.showText === false) {
      // 「没听清」先不给原文，保留听力挑战（计划 §5）
      flashHint('已隐藏原文：先只给声音，试试能不能听出来');
    }
  }

  // F05：隐藏/恢复「含原文」的区域。
  // 只控显隐（加 class），不改原 DOM 结构 —— 原 UI 完整保留。
  // 覆盖三处泄露原文的地方：
  //   1) #current-subtitle / #subtitle-translation  （当前句字幕）
  //   2) #subtitle-timeline 的 outline-preview      （内容大纲里的句子摘要）
  //   3) #reading-passage                            （整篇阅读正文）
  function applyTextVisibility(visible) {
    const show = visible !== false;
    document.body.classList.toggle('cet-hide-text', !show);
    // 点词查询浮层同样泄露原文：隐藏时把它关掉（不能只靠 CSS，
    // 否则它下次被点开又会出现）
    if (!show) {
      const pop = document.getElementById('word-popover');
      if (pop) {
        pop.classList.remove('open');
        pop.innerHTML = '';
        pop.style.display = 'none';
      }
    } else {
      const pop = document.getElementById('word-popover');
      if (pop) pop.style.display = '';
    }
    // 无障碍：隐藏时同步 aria-hidden，读屏也不会泄露原文。
    // 覆盖所有可能含原文的元素 —— 截图实测生词卡（#vocab-chips）
    // 和句子元信息（#subtitle-meta）也会漏出「displaying v. 展出」。
    ['current-subtitle', 'subtitle-translation', 'vocab-chips',
     'subtitle-meta', 'subtitle-position', 'reading-passage',
     'word-popover'].forEach((id) => {
      const n = document.getElementById(id);
      if (n) n.setAttribute('aria-hidden', show ? 'false' : 'true');
    });
    // 大纲里的句子摘要
    document.querySelectorAll('.outline-preview, .outline-title').forEach((n) => {
      n.setAttribute('aria-hidden', show ? 'false' : 'true');
    });
    const tl = document.getElementById('subtitle-timeline');
    if (tl) tl.setAttribute('data-cet-text-hidden', show ? '0' : '1');
  }

  function textIsHidden() {
    return document.body.classList.contains('cet-hide-text');
  }

  // F05：「听懂了」→ 换句流程。隐藏提示、恢复 1.0x 原速。
  function hideTextForTransfer() {
    applyTextVisibility(false);      // 隐藏字幕 / 大纲摘要 / 阅读正文
    restoreOriginalSpeed();          // 恢复 1.0x 原速
    const out = $('#cet-practice-out');
    if (out) {
      const note = el('div', 'cet-ai-note',
        '换句检验：原文与字幕已隐藏，播放已恢复 1.0x 原速。');
      note.setAttribute('data-cet-transfer-hidden', '1');
      out.appendChild(note);
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
      // F10：标记恢复后时间线也要画出来（原来只在列表里）
      ensureTimeline();
      renderTimelineMarks();
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
      // F10：播放器元素由 workspace.html 在 lessonData 加载后才创建，
      // 这时可能还不存在。等它就绪再挂时间线（只挂一次）。
      waitForPlayerThenTimeline();
    } catch (err) {
      if (err.status === 404) {
        // 不是四六级材料：静默跳过，不影响原页面
        return;
      }
      console.warn('[cet] 初始化失败', err);
    }
  }

  // F10：等待 #local-media-player 出现（最多 20 秒），然后挂载时间线。
  // 同时监听：页面切到精听/阅读会重建播放器，时间线要跟着重建。
  function waitForPlayerThenTimeline() {
    if (media()) {
      ensureTimeline();
      renderTimelineMarks();
    }
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      if (media()) {
        // 播放器已就绪：只要时间线不在 DOM 里就补挂（幂等）
        if (!$('#cet-timeline')) {
          ensureTimeline();
          renderTimelineMarks();
        }
        if (tries > 5) clearInterval(timer);   // 播放器出现后再多看几轮就收工
      } else if (tries > 100) {
        clearInterval(timer);                  // 20 秒还没播放器就放弃（不阻塞页面）
      }
    }, 200);
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
    // F08：预览/调整/改一下（自动化测试直接驱动，避免只能靠点按钮）
    previewMark, adjustMark, reopenMark, previewSentenceId,
    // F05：换句闭环
    renderTransfer, playTransferCandidate, restoreOriginalSpeed,
    applyTextVisibility, textIsHidden,
    // F10：时间线
    renderTimelineMarks,
  };
})();
