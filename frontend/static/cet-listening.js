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
    // 卡点修复 A-1：标记后是否**自动回听候选句**。
    // 默认 **关闭**：原先无条件自动回听会把播放位置 seek 回候选句起点，
    // 用户按完 M 立刻被拽走，连按几次全落在同一点（实测 3.74s → 0.058s）。
    // 能力保留 —— 想要「标记即回听」的用户可以把这个开关打开。
    autoplayKey: 'cet.marks.autoplay.v1',
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
    completedMarkIds: [],
    advanceTimer: null,
    practice: null,
    sessionId: null,
    listening: false,     // 兼容旧字段：等价于 S.step === 'blind'
    lastMarkAt: 0,        // 防连发
    // K1：三步状态机。null 表示模块尚未完成初始化（__cet.step 初始值）。
    step: null,           // 'blind' | 'review' | 'drill'
    stepReady: false,
    mounted: false,
    // 今日复习（K10）：在 review 步骤里打开复习会话，不改变三步持久化状态。
    reviewSessionActive: false,
    reviewReturnStep: 'blind',
    reviewEntryKey: '',
    reviewRevealed: false,
    // K9：保存本轮后只问一次「收藏到我的收藏」。
    favoriteAskSentence: null,
    favoriteAskedPracticeId: null,
    // 进入 drill 时只自动播放一次（key = practiceId:currentStepId）
    drillAutoPlayKey: null,
    lastStepEvent: null,
    // H3：?review=1 的自动复习只在本页生命周期触发一次，避免重复弹提示。
    reviewQueryStarted: false,
    reviewNotice: '',
    // F08：预览目标（markId → sentenceId）。预览只切试听、不落库；
    // 只有点「确认这句」才 PATCH /marks/{id} 写 confirmedSentenceId。
    previews: {},
    // F05：换句闭环状态
    transferPlayed: false,   // 候选原声是否已完整播过（后端要求先有 playedToEnd）
    transferDone: false,     // 本轮换句是否已作答
    transferCandidate: null, // 当前候选（含跨材料音频地址）
    // 卡点修复 A-1：标记后自动回听。默认 false（关闭），状态持久化到 localStorage。
    autoplayOnMark: false,
  };

  const STEP_KEYS = ['blind', 'review', 'drill'];
  const STEP_LABEL = { blind: '盲听做题', review: '对答案·找卡点', drill: '精练·复习' };
  // 卡点三态的一句话解释（悬停显示，界面上不占位置）
  const MARK_STATE_HINT = {
    pending: '系统找出的候选句，等你确认',
    ready: '已确认，还没练',
    done: '这句练过了',
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

  // ── 卡点修复 A-1：标记后自动回听开关 ──────────────────────
  // 默认关闭。开关状态存 localStorage（键 cet.marks.autoplay.v1，值为 '1'/'0'）。
  // 为什么单独一个键而不是塞进 cet.state.v2.<materialId>：
  // 这是**跨材料的使用习惯**（用户偏好），不是某份材料的练习进度；
  // 塞进按材料分键的状态里会导致换个材料又要重设一次。
  function loadAutoplayPref() {
    try {
      return localStorage.getItem(CFG.autoplayKey) === '1';
    } catch (_) { return false; }        // 隐私模式读不到 = 用默认值（关闭）
  }

  function saveAutoplayPref(on) {
    try {
      localStorage.setItem(CFG.autoplayKey, on ? '1' : '0');
    } catch (_) { /* 隐私模式可能失败，不阻塞 */ }
  }

  function setAutoplayOnMark(on, opts) {
    S.autoplayOnMark = !!on;
    saveAutoplayPref(S.autoplayOnMark);
    const box = $('#cet-autoplay-mark');
    if (box) box.checked = S.autoplayOnMark;
    // 勾完开关后把焦点移走：焦点留在复选框上时，后续键盘操作（含底座
    // Space / R / L）都会被当成输入框输入而失效。标记键已取消键盘绑定，
    // 但这条 blur 对底座快捷键仍然必要。
    if (opts && opts.blur && document.activeElement && document.activeElement.blur) {
      document.activeElement.blur();
    }
    if (!(opts && opts.silent)) {
      flashHint(S.autoplayOnMark
        ? '已开启「标记后自动回听」：点「没听清」会跳到候选句播放'
        : '已关闭「标记后自动回听」：点「没听清」只记标记，不打断当前播放位置');
    }
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

  // ── W 接口适配（任务书 §2 的 K↔W 契约）──────────────────
  // K 不直接改播放器状态、不直接写 W 的右栏/工具栏 DOM；全部经 __ws。
  // 下面的 fallback 仅用于「W 新接口尚未接入」的自测环境，生产集成时全部走 __ws。
  function ws() { return window.__ws || null; }

  function wsFn(name) {
    const api = ws();
    if (!api || typeof api[name] !== 'function') return null;
    return api[name].bind(api);
  }

  function wsSentences() {
    const api = ws();
    if (api && Array.isArray(api.sentences) && api.sentences.length) return api.sentences;
    return S.sentences;
  }

  function sentenceStart(sent) {
    const value = sent && (sent.startSeconds != null ? sent.startSeconds : sent.startSec != null ? sent.startSec : sent.start);
    return Number(value);
  }
  function sentenceEnd(sent) {
    const value = sent && (sent.endSeconds != null ? sent.endSeconds : sent.endSec != null ? sent.endSec : sent.end);
    return Number(value);
  }

  // K 的句子对象来自 /materials/by-lesson；__ws.sentences 是 W 的句子单位。
  // 两者必须按 index + 文本/时间对应，不能拿 K 的数组下标直接混用。
  function sentenceIndexFor(sent) {
    if (!sent) return -1;
    const list = wsSentences();
    if (!list.length) return -1;
    let index = list.findIndex((unit) => Number(unit.index) === Number(sent.index)
      && String(unit.text || '') === String(sent.text || ''));
    if (index < 0) {
      const start = sentenceStart(sent);
      const end = sentenceEnd(sent);
      index = list.findIndex((unit) => Number.isFinite(start) && Number.isFinite(end)
        && Math.abs(Number(unit.start != null ? unit.start : unit.startSeconds) - start) < 0.05
        && Math.abs(Number(unit.end != null ? unit.end : unit.endSeconds) - end) < 0.05);
    }
    if (index < 0) {
      index = list.findIndex((unit) => String(unit.text || '').trim() === String(sent.text || '').trim());
    }
    return index;
  }

  function playSentenceBySentence(sent, opts) {
    if (!sent) return Promise.resolve('interrupted');
    const index = sentenceIndexFor(sent);
    if (index < 0) {
      console.warn('[cet] 无法把句子映射到 __ws.sentences', sent);
      return Promise.resolve('interrupted');
    }
    const play = wsFn('playSentence');
    if (!play) {
      flashHint('播放接口未就绪：请刷新页面后重试', true);
      return Promise.resolve('interrupted');
    }
    try {
      return Promise.resolve(play(index, opts || {}));
    } catch (err) {
      console.warn('[cet] playSentence 调用失败', err);
      return Promise.resolve('interrupted');
    }
  }

  function playSentenceByRange(startSec, endSec, opts) {
    const start = Number(startSec);
    const end = Number(endSec);
    const list = wsSentences();
    let index = -1;
    if (Number.isFinite(start) && Number.isFinite(end)) {
      index = list.findIndex((unit) => Math.abs(Number(unit.start != null ? unit.start : unit.startSeconds) - start) < 0.12
        && Math.abs(Number(unit.end != null ? unit.end : unit.endSeconds) - end) < 0.12);
    }
    if (index < 0 && Number.isFinite(start)) {
      // 证据句偶尔只给起点附近，退化为「包含该起点的句子」。
      index = list.findIndex((unit) => Number(unit.start != null ? unit.start : unit.startSeconds) <= start + 0.02
        && start < Number(unit.end != null ? unit.end : unit.endSeconds));
    }
    if (index < 0) return Promise.resolve('interrupted');
    const play = wsFn('playSentence');
    if (!play) return Promise.resolve('interrupted');
    try { return Promise.resolve(play(index, opts || {})); } catch (_) { return Promise.resolve('interrupted'); }
  }

  function stopSentencePlayback() {
    const stop = wsFn('stopSentence');
    if (stop) {
      try { stop(); } catch (_) { /* 没有播放时忽略 */ }
    }
  }

  // 统一「原文隐藏」入口。K 在三步流程内不再直接调用 applyTextVisibility。
  function setTranscriptHiddenCompat(hidden) {
    const api = ws();
    if (api && typeof api.setTranscriptHidden === 'function') {
      try { api.setTranscriptHidden(!!hidden); return; } catch (err) { console.warn('[cet] setTranscriptHidden 调用失败', err); }
    }
    legacyApplyTextVisibility(!hidden);
  }

  function currentTranscriptHidden() {
    const api = ws();
    if (api && typeof api.transcriptHidden === 'boolean') return api.transcriptHidden;
    return document.body.classList.contains('cet-hide-text');
  }

  function syncTranscriptVisibility() {
    // K2：blind 整个步骤隐藏原文；K10 今日复习盲听阶段也隐藏；
    // K3 review（对答案）和 K6 drill 显示原文。
    const hidden = S.step === 'blind'
      || (S.step === 'review' && S.reviewSessionActive && !S.reviewRevealed);
    setTranscriptHiddenCompat(hidden);
  }

  function mountStepPanelCompat(stepKey, element) {
    const fn = wsFn('mountStepPanel');
    if (fn) {
      try { fn(stepKey, element); return; } catch (err) { console.warn('[cet] mountStepPanel 调用失败', err); }
    }
    // fallback：W 未接入时把面板挂到右栏，便于本地自测。
    let host = document.getElementById('cet-step-panels-fallback');
    if (!host) {
      const side = $('.side-column');
      if (!side) return;
      host = el('section', 'cet-step-panels-fallback');
      host.id = 'cet-step-panels-fallback';
      side.insertBefore(host, side.firstChild);
    }
    element.dataset.cetStep = stepKey;
    element.hidden = stepKey !== S.step;
    host.appendChild(element);
  }

  function setStepActionsCompat(element) {
    const fn = wsFn('setStepActions');
    if (fn) {
      try { fn(element); return; } catch (err) { console.warn('[cet] setStepActions 调用失败', err); }
    }
    let host = document.getElementById('cet-step-actions-fallback');
    if (!host) {
      const controls = $('.learning-controls');
      if (!controls) return;
      host = el('div', 'cet-step-actions-fallback');
      host.id = 'cet-step-actions-fallback';
      controls.appendChild(host);
    }
    host.innerHTML = '';
    host.appendChild(element);
  }

  function updateFallbackPanelVisibility() {
    const host = document.getElementById('cet-step-panels-fallback');
    if (!host) return;
    [...host.children].forEach((child) => {
      child.hidden = child.dataset.cetStep !== S.step;
    });
  }

  function registerHotkeyHelp() {
    // 标记已取消键盘绑定（用户决定只用鼠标点按钮），因此不再向快捷键面板
    // 注册 M；帮助面板只显示底座自己的 Space / ← → / R / L。
    // 函数保留是为了不动 W 侧的 §2 接口调用点。
  }

  // ── 三步面板与工具栏（K1/K12）──────────────────────────
  function buildStepPanel(stepKey) {
    const section = el('section', `cet-panel cet-step-panel cet-step-panel-${stepKey}`);
    section.id = `cet-${stepKey}-panel`;
    section.dataset.cetStep = stepKey;
    const head = el('div', 'cet-panel-head');
    head.id = `cet-${stepKey}-head`;
    const body = el('div', 'cet-step-panel-body');
    body.id = `cet-${stepKey}-body`;
    section.appendChild(head);
    section.appendChild(body);
    return section;
  }

  function mount() {
    if (S.mounted) return;
    const host = $('.learning-controls');
    if (!host) return;
    S.mounted = true;

    // 卡点修复 A-1：保留用户偏好读取；新三步流程不再提供旧工具栏开关。
    S.autoplayOnMark = loadAutoplayPref();
    const markBtn = el('button', 'cet-btn cet-mark-btn', '🚩 没听清');
    markBtn.type = 'button';
    markBtn.onclick = markCurrentPosition;
    (host.querySelector('.ws-control-row--primary') || host).appendChild(markBtn);

    // 标记列表容器：仍放在字幕卡下方（K2/K4 的卡点行）。
    const card = $('.subtitle-card');
    if (card && !$('#cet-marks')) {
      const box = el('div', 'cet-marks');
      box.id = 'cet-marks';
      box.hidden = true;
      card.appendChild(box);
    }

    // K6/K10：三块面板一次注册给 W，由 W 的「本步骤」tab 按 __cet.step 切换。
    ['blind', 'review', 'drill'].forEach((key) => {
      mountStepPanelCompat(key, buildStepPanel(key));
    });

    // K11：M 只注册帮助；按键监听仍在 K。
    registerHotkeyHelp();

    // K12：旧的「开始/结束卡点练习」二态按钮不再创建，步骤由 W2 步骤条驱动。
    renderStepActions();
    bindTimelineOutsideClick();
  }

  function stepPrimaryLabel() {
    if (S.step === 'blind') {
      if (S.questions.length) return S.attempt ? '查看交卷结果 →' : '交卷';
      return '显示原文，确认卡点';
    }
    if (S.step === 'review') {
      if (S.reviewSessionActive) {
        return reviewState && reviewState.status === 'complete' ? '返回学习' : '结束复习';
      }
      if (S.practice && S.practice.status !== 'complete') return '继续精练 →';
      return '进入精练 →';
    }
    if (S.step === 'drill') {
      return '';
    }
    return '';
  }

  function stepPrimaryDisabled() {
    if (S.step === 'review' && !S.reviewSessionActive) {
      const ready = S.marks.some((m) => m.confirmedSentenceId);
      return !ready && !(S.practice && S.practice.status !== 'complete');
    }
    return false;
  }

  function onStepPrimary() {
    if (S.step === 'blind') {
      if (S.questions.length) {
        if (S.attempt) setStep('review');
        else submitAttempt();
      } else {
        setStep('review');
      }
      return;
    }
    if (S.step === 'review') {
      if (S.reviewSessionActive) {
        if (reviewState && reviewState.status === 'complete') exitReviewSession();
        else reviewAction('finish');
        return;
      }
      if (S.practice && S.practice.status !== 'complete') { setStep('drill'); return; }
      enterDrill();
      return;
    }
    if (S.step === 'drill') {
      setStep('review');
    }
  }

  function renderStepActions() {
    const wrap = el('div', 'cet-step-actions');
    wrap.id = 'cet-step-actions';
    const primary = el('button', 'cet-btn cet-step-primary', stepPrimaryLabel());
    primary.id = 'cet-step-primary';
    primary.disabled = stepPrimaryDisabled();
    primary.onclick = onStepPrimary;
    if (S.step !== 'drill') wrap.appendChild(primary);

    const reviewBtn = el('button', 'cet-btn-sm cet-step-review-btn', '今日复习');
    reviewBtn.id = 'cet-review-btn';
    reviewBtn.onclick = startReview;
    wrap.appendChild(reviewBtn);

    const hint = el('span', 'cet-hint', stepHintText());
    hint.id = 'cet-hint';
    wrap.appendChild(hint);
    setStepActionsCompat(wrap);
  }

  function stepHintText() {
    if (S.step === 'blind') {
      return S.questions.length
        ? '先盲听；听到没听清的地方就点「🚩 没听清」，做完题后交卷。'
        : '先盲听；听到没听清的地方就点「🚩 没听清」，然后点「显示原文，确认卡点」。';
    }
    if (S.step === 'review') {
      if (S.reviewSessionActive) return '先盲听这一处，点「显示原文」后再核对。';
      return S.questions.length ? '交卷后查看成绩与原声证据；确认卡点后进入精练。' : '在卡点列表确认真正卡住的句子。';
    }
    if (S.step === 'drill') return '带练中：原句、慢放、分块都由统一播放器播放。';
    return '';
  }

  function setStep(step, opts) {
    if (!STEP_KEYS.includes(step)) return;
    const options = opts || {};
    const previous = S.step;
    S.step = step;
    if (step !== 'review') S.reviewNotice = '';
    S.listening = step === 'blind';
    document.body.classList.toggle('cet-listening', !!step);
    if (!options.silent) stopSentencePlayback();
    updateFallbackPanelVisibility();
    renderStepUI();
    syncTranscriptVisibility();
    save();
    emitStepChange(!!options.force);
    syncReviewStepBar();
    if (step === 'drill' && previous !== 'drill' && !options.skipAutoplay) {
      autoPlayDrillOnce();
    }
  }

  function renderStepUI() {
    const markBtn = $('.cet-mark-btn');
    if (markBtn) markBtn.hidden = S.step !== 'blind';
    renderBlindPanel();
    renderReviewPanel();
    renderDrillPanel();
    renderStepActions();
    renderMarks();
    renderTimelineMarks();
  }

  function emitStepChange(force) {
    if (!S.stepReady && !force) return;
    const detail = {
      step: S.step,
      hasQuestions: S.questions.length > 0,
      submitted: !!S.attempt,
    };
    const key = JSON.stringify(detail);
    if (!force && S.lastStepEvent === key) return;
    S.lastStepEvent = key;
    document.dispatchEvent(new CustomEvent('cet:step-change', { detail }));
  }

  function syncReviewStepBar() {
    // 今日复习仍使用 review 面板，但步骤条保持在「③ 精练·复习」。
    const bar = $('#ws-step-bar');
    if (!bar || !S.reviewSessionActive) return;
    bar.dataset.step = 'drill';
    bar.querySelectorAll('[data-ws-step]').forEach((item) => {
      const active = item.dataset.wsStep === 'drill';
      item.classList.toggle('active', active);
      if (active) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
  }

  // 兼容旧测试/调用：旧语义 on=true 进入盲听，on=false 退出到对答案（不是回到普通页面）。
  function setListening(on) {
    if (on) {
      setStep('blind');
      return;
    }
    setStep('review');
  }

  // ── 标记入口：只用鼠标点「🚩 没听清」，不再绑 M 键 ─────────────
  // 2026-11-08 用户决定：M 键长期有问题（焦点在按钮/输入框上时被吞、
  // 和浏览器/输入法扩展冲突、演示时观众看不到按键），干脆取消键盘绑定，
  // 只保留工具栏那个大按钮。markCurrentPosition() 仍是唯一标记入口，
  // 按钮 onclick 直接调它，能力与之前完全一致。
  // Space / ← → / R / L 仍归底座 cet-player.js，不受影响。

  function markCurrentPosition() {
    if (S.step !== 'blind') return;
    const m = media();
    if (!m) return;
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
      // 卡点修复 A-2：后端有 1.5s 去重（ABS(raw_seconds-?)<=1.5 时返回**已有**标记）。
      // 原代码无条件 push —— 拿到同一个 markId 也照样入列，于是 UI 显示 5 个标记
      // 而库里只有 2 条（实测：S.marks.length 5 / cet_marks 2 行）。
      // 现在按 markId 先查：已存在就就地更新，不新增行。
      const idx = S.marks.findIndex((x) => x.markId === mark.markId);
      const isNew = idx < 0;
      if (isNew) {
        S.marks.push(mark);
      } else {
        // 用服务端最新值覆盖本地旧值（同一条标记，不是新标记）
        Object.assign(S.marks[idx], mark);
      }
      renderMarks();
      const blindCount = $('#cet-blind-mark-count');
      if (blindCount) blindCount.textContent = `已标记 ${S.marks.length} 处卡点`;
      // 卡点修复 A-1：**默认不再自动回听**。
      // 原代码无条件 previewMark(..., {autoplay:true})，会把播放位置 seek 回候选句起点：
      // 实测连续标记时 3.74s，120ms 后变成 0.058s，连按 3 次全落在同一句。
      // 能力保留 —— 开关打开时行为与原来完全一致。
      if (S.autoplayOnMark) {
        previewMark(mark, mark.suggestedSentenceId, { autoplay: true });
      }
      if (isNew) {
        flashHint(`已标记 ${fmtClock(mark.pressedAtSec)}`, false, 1000);
      } else {
        // 去重命中：明确告诉用户没有新增，避免「按了没反应 / 按了出两条」的困惑
        flashHint(`与 ${fmt(mark.pressedAtSec)} 的标记是同一处（1.5s 内重复），未新增`);
      }
    } catch (err) {
      flashHint(`标记失败：${err.message}（可重试）`, true);
    }
  }

  function flashHint(text, isError, duration) {
    const hint = $('#cet-hint');
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle('error', !!isError);
    clearTimeout(flashHint._t);
    flashHint._t = setTimeout(() => {
      hint.textContent = stepHintText();
      hint.classList.remove('error');
    }, duration || 2600);
  }

  // ── 标记渲染（三态：待确认 / 待练 / 本轮已练）──────────
  function markState(m) {
    // F08：「改一下」把已确认的标记退回待确认态，允许重新预览与覆盖。
    if (m.__amending) return 'pending';
    if (m.confirmedSentenceId) {
      return S.completedMarkIds.includes(m.markId) || (S.practice && S.practice.markId === m.markId && S.practice.status === 'complete')
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

  function fmtClock(sec) {
    const total = Math.max(0, Math.floor(Number(sec) || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function playMark(m, sentenceId) {
    const sent = findSentence(sentenceId || previewSentenceId(m));
    if (!sent) { flashHint('没有可回听的候选句', true); return; }
    playSentenceBySentence(sent, {});
  }

  function adjustConfirmedMark(m, delta) {
    S.previews[m.markId] = m.confirmedSentenceId || m.suggestedSentenceId;
    m.__amending = true;
    adjustMark(m, delta);
    renderReviewPanel();
    renderStepActions();
  }

  /**
   * 把卡点「⋯」菜单定位到按钮旁边（position:fixed，不受 .watch-column 裁剪）。
   * 下方空间不够就向上弹；左右贴边时收回视口内。
   */
  function positionMarkMenu(summary, list) {
    const btn = summary.getBoundingClientRect();
    const box = list.getBoundingClientRect();
    const gap = 6;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const spaceBelow = vh - btn.bottom;
    const openUp = spaceBelow < box.height + gap && btn.top > spaceBelow;
    let top = openUp ? btn.top - box.height - gap : btn.bottom + gap;
    top = Math.max(8, Math.min(top, vh - box.height - 8));
    let left = btn.right - box.width;
    left = Math.max(8, Math.min(left, vw - box.width - 8));
    list.style.top = `${Math.round(top)}px`;
    list.style.left = `${Math.round(left)}px`;
    list.style.right = 'auto';
  }

  function buildMarkMenu(m, st) {
    const det = el('details', 'cet-mark-menu');
    const summary = el('summary', 'cet-mark-menu-btn', '⋯');
    summary.title = '更多操作';
    summary.setAttribute('aria-label', '更多操作');
    det.appendChild(summary);
    const list = el('div', 'cet-mark-menu-list');
    // 打开时先渲染再定位（hidden 状态下量不到真实尺寸）
    det.addEventListener('toggle', () => {
      if (det.open) {
        window.requestAnimationFrame(() => positionMarkMenu(summary, list));
      } else {
        list.style.top = '';
        list.style.left = '';
        list.style.right = '';
      }
    });
    const add = (label, fn, disabled, title) => {
      const btn = el('button', 'cet-mark-menu-item', label);
      if (disabled) {
        btn.disabled = true;
        if (title) btn.title = title;
      } else {
        btn.onclick = (event) => {
          event.preventDefault();
          det.open = false;
          fn();
        };
      }
      list.appendChild(btn);
    };
    if (st === 'pending') {
      add('前一句', () => adjustMark(m, -1));
      add('后一句', () => adjustMark(m, 1));
    }
    if (st === 'ready') {
      const locked = m.inPractice === true;
      add('前一句', () => adjustConfirmedMark(m, -1), locked, '已开始带练，不能改句');
      add('后一句', () => adjustConfirmedMark(m, 1), locked, '已开始带练，不能改句');
      add('改一下', () => reopenMark(m), locked, '已开始带练，不能改句');
    }
    add('删除', () => deleteMark(m), m.inPractice === true, '该标记已开始带练，不能删除');
    det.appendChild(list);
    return det;
  }

  // K2/K4：卡点行按步骤收窄
  //   blind  —— 只露「标记于 m:ss」和「回听」，不显示候选句原文、无确认按钮
  //   review —— 额外露主动作（确认这句 / 开始带练），其余进「⋯」菜单
  //   drill  —— 保留回听与状态，不抢带练面板的主动作
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
    box.appendChild(el('div', 'cet-marks-title', `卡点标记（${S.marks.length}）`));

    S.marks.forEach((m) => {
      const st = markState(m);
      const row = el('div', `cet-mark cet-mark-${st}`);
      const when = el('span', 'cet-mark-time', `标记于 ${fmtClock(m.pressedAtSec)}`);
      when.title = '点「没听清」那一刻的播放位置（不是候选句位置）';
      row.appendChild(when);

      const shownId = st === 'pending' ? previewSentenceId(m) : m.confirmedSentenceId;
      const sent = findSentence(shownId);

      // K2：blind 步骤不泄露候选句原文，也不出现确认按钮。
      if (S.step !== 'blind') {
        const label = { pending: '待确认', ready: '待练', done: '本轮已练' }[st];
        const badge = el('span', `cet-mark-badge cet-mark-badge-${st}`, label);
        // 一句话解释三个状态（用户反馈「待确认」看不懂）
        badge.title = MARK_STATE_HINT[st];
        row.appendChild(badge);
        if (sent) {
          const tag = el('span', 'cet-mark-preview-tag',
            `${st === 'pending' ? '候选句' : '确认句'} ${fmt(sent.startSeconds)}–${fmt(sent.endSeconds)}`);
          tag.title = st === 'pending' ? '按回溯窗口吸附出来的候选句，还没确认' : '已确认的句子';
          row.appendChild(tag);
          const reviewEntries = reviewState && reviewState.entries || [];
          const currentReview = reviewEntries.find((entry) => entry.status === 'current')
            || reviewEntries.find((entry) => entry.status === 'pending');
          const reviewing = S.reviewSessionActive && currentReview && currentReview.item
            && currentReview.item.sentence && currentReview.item.sentence.sentenceId === shownId;
          row.appendChild(el('span', 'cet-mark-sentence', reviewing ? '复习中' : sent.text));
        } else {
          row.appendChild(el('span', 'cet-mark-sentence cet-mark-sentence-none', '（无可用候选句）'));
        }
      }

      const acts = el('span', 'cet-mark-actions');
      const play = el('button', 'cet-btn-sm', '回听');
      play.onclick = () => playMark(m, shownId);
      acts.appendChild(play);

      if (S.step === 'review') {
        if (st === 'pending') {
          const conf = el('button', 'cet-btn-sm cet-btn-confirm', '确认这句');
          conf.onclick = () => confirmMark(m, previewSentenceId(m));
          acts.appendChild(conf);
        } else if (st === 'ready') {
          const start = el('button', 'cet-btn-sm cet-btn-confirm', '开始带练');
          start.onclick = () => startPractice(m);
          acts.appendChild(start);
        }
      }
      if (S.step !== 'blind') acts.appendChild(buildMarkMenu(m, st));

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

  async function confirmMark(m, sentenceId, opts) {
    const options = opts || {};
    const sid = sentenceId || previewSentenceId(m);
    if (!sid) { flashHint('没有可确认的句子', true); return; }
    try {
      const updated = await api('PATCH', `/marks/${m.markId}`, { confirmedSentenceId: sid });
      Object.assign(m, updated);
      // 确认后清掉预览态：后续「回听」应播确认句
      delete S.previews[m.markId];
      delete m.__amending;
      renderMarks();
      renderReviewPanel();
      renderStepActions();
      flashHint('已确认卡点句子（还可以点「改一下」重新调整）');
    } catch (err) {
      if (err.code === 'ATTEMPT_REQUIRED') {
        // K1 无题材料：后端仍要求有 attempt。静默补一份空 attempt 再重试，
        // 用户侧不需要看到「交卷」；有题材料仍明确提示先交卷。
        if (!options.retried && !S.questions.length && await ensureAttemptForNoQuestions()) {
          await confirmMark(m, sid, { retried: true });
          return;
        }
        flashHint(S.questions.length ? '请先交卷，再确认卡点句子' : '确认失败：当前材料无法建立作答记录', true);
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
      renderReviewPanel();
      renderStepActions();
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
    renderReviewPanel();
    renderStepActions();
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
    const api = ws();
    if (api && Number.isFinite(Number(api.duration)) && Number(api.duration) > 0) return Number(api.duration);
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

  // K5：时间条改成小圆点 + hover 提示；相距 < 2% 宽的标记合并为计数气泡。
  function timelineItems() {
    const dur = timelineDuration();
    if (!dur) return [];
    return S.marks.map((m) => {
      const st = markState(m);
      const sentId = st === 'pending' ? previewSentenceId(m) : m.confirmedSentenceId;
      const sent = findSentence(sentId);
      const sec = sent ? Number(sent.startSeconds) : (Number(m.pressedAtSec) || 0);
      return {
        mark: m,
        state: st,
        sentenceId: sentId,
        sentence: sent,
        sec,
        pct: Math.min(100, Math.max(0, (sec / dur) * 100)),
      };
    }).sort((a, b) => a.pct - b.pct);
  }

  function groupTimelineItems(items) {
    const groups = [];
    items.forEach((item) => {
      const last = groups[groups.length - 1];
      if (last && item.pct - last.firstPct < 2) {
        last.items.push(item);
      } else {
        groups.push({ firstPct: item.pct, items: [item] });
      }
    });
    return groups;
  }

  function closeTimelinePopover() {
    const pop = $('#cet-timeline-popover');
    if (pop) pop.remove();
  }

  function bindTimelineOutsideClick() {
    if (bindTimelineOutsideClick._bound) return;
    bindTimelineOutsideClick._bound = true;
    document.addEventListener('click', (event) => {
      if (event.target.closest && (event.target.closest('#cet-timeline-popover') || event.target.closest('.cet-timeline-bubble') || event.target.closest('.cet-timeline-dotmark'))) return;
      closeTimelinePopover();
    });
  }

  function openTimelinePopover(anchor, items) {
    closeTimelinePopover();
    const wrap = anchor.closest('.cet-timeline') || anchor.parentNode;
    if (!wrap) return;
    const pop = el('div', 'cet-timeline-popover');
    pop.id = 'cet-timeline-popover';
    pop.style.left = anchor.style.left;
    items.forEach((item) => {
      const row = el('div', `cet-timeline-popover-row cet-timeline-popover-${item.state}`);
      row.appendChild(el('span', 'cet-timeline-popover-time', `标记于 ${fmtClock(item.mark.pressedAtSec)}`));
      const meta = TIMELINE_STATE[item.state];
      row.appendChild(el('span', 'cet-timeline-popover-state', meta.label));
      const play = el('button', 'cet-timeline-popover-play', '回听');
      play.onclick = (event) => {
        event.stopPropagation();
        playMark(item.mark, item.sentenceId);
      };
      row.appendChild(play);
      pop.appendChild(row);
    });
    wrap.appendChild(pop);
  }

  function renderTimelineMarks() {
    const wrap = ensureTimeline();
    if (!wrap) return;
    const track = $('#cet-timeline-track');
    if (!track) return;
    const dur = timelineDuration();
    track.innerHTML = '';
    closeTimelinePopover();
    if (!dur) return;
    track.setAttribute('data-cet-duration-sec', String(Number(dur.toFixed(3))));
    if (!S.marks.length) {
      track.appendChild(el('div', 'cet-timeline-empty', '暂无卡点标记（播放时点「🚩 没听清」标记）'));
      return;
    }

    groupTimelineItems(timelineItems()).forEach((group) => {
      const first = group.items[0];
      const meta = TIMELINE_STATE[first.state];
      const bubble = group.items.length > 1;
      const btn = el('button', bubble ? 'cet-timeline-bubble' : 'cet-timeline-dotmark');
      btn.type = 'button';
      btn.style.left = `${first.pct}%`;
      btn.setAttribute('data-cet-mark', '1');
      btn.setAttribute('data-cet-mark-count', String(group.items.length));
      btn.setAttribute('data-cet-mark-sec', String(Number(first.sec.toFixed(3))));
      btn.setAttribute('data-cet-mark-state', first.state);
      btn.setAttribute('data-cet-mark-sentence', first.sentenceId || '');
      btn.setAttribute('data-cet-mark-pressed', String(Number((Number(first.mark.pressedAtSec) || 0).toFixed(3))));
      const title = bubble
        ? `${group.items.length} 个卡点：` + group.items.map((item) => `${fmtClock(item.mark.pressedAtSec)} ${TIMELINE_STATE[item.state].label}`).join('；')
        : (first.sentence
          ? `${fmtClock(first.mark.pressedAtSec)} 标记 · ${meta.label}（${fmt(first.sentence.startSeconds)}–${fmt(first.sentence.endSeconds)}）`
          : `${fmtClock(first.mark.pressedAtSec)} 标记 · ${meta.label}（无候选句）`);
      btn.title = title;
      btn.setAttribute('aria-label', title);
      if (bubble) btn.appendChild(el('span', 'cet-timeline-bubble-count', String(group.items.length)));
      btn.onclick = (event) => {
        event.stopPropagation();
        openTimelinePopover(btn, group.items);
      };
      track.appendChild(btn);
    });
  }


  function scoreLabel(at) {
    const graded = Number(at && at.gradedCount);
    const score = Number(at && at.score);
    if (Number.isFinite(graded) && graded > 0) {
      return `${Number.isFinite(score) ? score : 0} / ${graded}`;
    }
    return '暂无判分';
  }

  // K2：blind 面板。有题材料在这里做题；无题材料只做盲听提示。
  function renderBlindPanel() {
    const head = $('#cet-blind-head');
    const body = $('#cet-blind-body');
    if (!head || !body) return;
    head.innerHTML = '';
    head.appendChild(el('strong', null, S.questions.length ? '盲听做题' : '盲听标记'));
    if (S.questions.length) {
      const state = el('span', 'cet-panel-state', S.attempt ? `已交卷 · ${scoreLabel(S.attempt)}` : '未交卷');
      state.id = 'cet-attempt-state';
      if (S.attempt) state.classList.add('done');
      head.appendChild(state);
    }
    body.innerHTML = '';
    body.appendChild(el('div', 'cet-step-lead', S.questions.length
      ? '先盲听；听到没听清的地方就点「🚩 没听清」。做完题后点下方「交卷」。'
      : '先盲听；听到没听清的地方就点「🚩 没听清」。准备好后点下方「显示原文，确认卡点」。'));
    if (S.questions.length) {
      const list = el('div', 'cet-question-list');
      list.id = 'cet-question-list';
      body.appendChild(list);
      renderQuestions();
      // 题目列表末尾也放一个「交卷」：手机上题目很长，做完最后一题时
      // 工具栏的交卷按钮已经滚到屏幕外（实测 375 宽时 top=-558px），
      // 用户会找不到交卷入口。这里放一个等价入口，两个按钮共用同一处理函数。
      if (!S.attempt) {
        const footRow = el('div', 'cet-question-foot');
        const footSubmit = el('button', 'cet-btn cet-question-submit', '交卷');
        footSubmit.id = 'cet-submit-foot';
        footSubmit.onclick = onStepPrimary;
        footRow.appendChild(footSubmit);
        const answered = Object.keys(S.answers || {}).length;
        footRow.appendChild(el('span', 'cet-question-foot-note',
          answered ? `已作答 ${answered} / ${S.questions.length} 题` : '还没作答，选完再交卷'));
        body.appendChild(footRow);
      }
      if (S.attempt) {
        const link = el('button', 'cet-btn-sm cet-inline-link', '查看交卷结果 →');
        link.onclick = () => setStep('review');
        body.appendChild(link);
      }
    } else {
      const count = el('div', 'cet-blind-count', `已标记 ${S.marks.length} 处卡点`);
      count.id = 'cet-blind-mark-count';
      body.appendChild(count);
    }
  }

  // 题目列表末尾的「已作答 x / y 题」提示：选一项就更新，用户能看到还差几题
  function updateQuestionFootNote() {
    const note = document.querySelector('.cet-question-foot-note');
    if (!note) return;
    const answered = Object.keys(S.answers || {}).length;
    const total = (S.questions || []).length;
    note.textContent = answered ? `已作答 ${answered} / ${total} 题` : '还没作答，选完再交卷';
  }

  function renderQuestions() {
    const list = $('#cet-question-list');
    if (!list) return;
    list.innerHTML = '';
    if (!S.questions.length) {
      list.appendChild(el('div', 'cet-empty', '本材料暂无题目'));
      return;
    }
    S.questions.forEach((q, qIndex) => {
      const item = el('div', 'cet-q');
      item.dataset.cetQuestionId = q.questionId || '';
      item.appendChild(el('div', 'cet-q-stem', `第 ${q.number || qIndex + 1} 题　${q.stem}`));
      const opts = el('div', 'cet-q-opts');
      const selected = S.answers[q.questionId];
      q.options.forEach((opt, i) => {
        const b = el('button', 'cet-opt', `${'ABCDE'[i]}. ${opt}`);
        b.type = 'button';
        if (selected === i) b.classList.add('sel');
        if (S.attempt) b.disabled = true;
        b.onclick = () => {
          S.answers[q.questionId] = i;
          opts.querySelectorAll('.cet-opt').forEach((x) => x.classList.remove('sel'));
          b.classList.add('sel');
          updateQuestionFootNote();
        };
        opts.appendChild(b);
      });
      item.appendChild(opts);
      list.appendChild(item);
    });
  }

  // K3：交卷后进入 review；无题材料不做可见交卷，但确认卡点前会静默补一份
  // 空 attempt 以满足后端 ATTEMPT_REQUIRED 规则（见 ensureAttemptForNoQuestions）。
  async function submitAttempt() {
    if (!S.questions.length) {
      const ok = await ensureAttemptForNoQuestions();
      if (ok) setStep('review');
      return;
    }
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
      save();
      emitStepChange(true);
      setStep('review');
      flashHint(`已交卷 · ${scoreLabel(at)}，可以确认卡点句子了`);
    } catch (err) {
      flashHint(`交卷失败：${err.message}`, true);
    }
  }

  async function ensureAttemptForNoQuestions() {
    if (S.attempt) return true;
    if (S.questions.length) return false;
    try {
      const at = await api('POST', '/attempts', {
        materialId: S.materialId,
        materialVersion: S.materialVersion,
        listeningSessionId: sessionId(),
        answers: [],
      });
      S.attempt = at;
      save();
      emitStepChange(true);
      return true;
    } catch (err) {
      flashHint(`无法进入确认：${err.message}`, true);
      return false;
    }
  }

  // K3：review 面板。有题材料显示 4 / 4、未知答案单列；无题材料显示确认说明。
  function renderReviewPanel() {
    const head = $('#cet-review-head');
    const body = $('#cet-review-body');
    if (!head || !body) return;
    head.innerHTML = '';
    head.appendChild(el('strong', null, S.reviewSessionActive ? '今日复习' : '对答案·找卡点'));
    body.innerHTML = '';
    // H3：从首页「今日复习」进入但暂无待复习条目时，保持 review 步骤并给出持久提示。
    if (!S.reviewSessionActive && S.reviewNotice) {
      body.appendChild(el('div', 'cet-ai-note', S.reviewNotice));
    }
    if (S.reviewSessionActive) {
      renderReviewSession(body);
      return;
    }
    if (!S.questions.length) {
      body.appendChild(el('div', 'cet-step-lead',
        '原文已显示。请在下方卡点列表里点「前一句 / 后一句」试听，再点「确认这句」选中真正卡住的句子。'));
      const confirmed = S.marks.filter((m) => m.confirmedSentenceId).length;
      body.appendChild(el('div', 'cet-review-note', confirmed
        ? `已确认 ${confirmed} 个卡点，可以进入精练。`
        : '还没有确认的卡点句子。'));
      const back = el('button', 'cet-btn-sm cet-inline-link', '← 回到盲听');
      back.onclick = () => setStep('blind');
      body.appendChild(back);
      return;
    }
    if (!S.attempt) {
      body.appendChild(el('div', 'cet-step-lead', '还没有交卷，先完成盲听做题。'));
      const back = el('button', 'cet-btn-sm cet-inline-link', '← 回到盲听做题');
      back.onclick = () => setStep('blind');
      body.appendChild(back);
      return;
    }
    const card = el('div', 'cet-score-card');
    card.appendChild(el('span', 'cet-score-label', '得分'));
    card.appendChild(el('strong', 'cet-score-value', scoreLabel(S.attempt)));
    body.appendChild(card);
    if (Number(S.attempt.unknownCount) > 0) {
      body.appendChild(el('div', 'cet-review-note cet-unknown-note',
        `另 ${Number(S.attempt.unknownCount)} 题无参考答案，不计分`));
    }
    const results = el('div', 'cet-review-results');
    results.id = 'cet-review-results';
    body.appendChild(results);
    renderAttemptResult(S.attempt, results);
    body.appendChild(el('div', 'cet-review-note',
      '点「▶ 听原声证据」可回听出处；卡点列表在下方。'));
  }

  function renderAttemptResult(at, target) {
    const list = typeof target === 'string' ? $(target) : (target || $('#cet-review-results'));
    if (!list) return;
    list.innerHTML = '';
    const results = (at && at.results) || [];
    if (!results.length) {
      list.appendChild(el('div', 'cet-empty', '暂无判题结果'));
      return;
    }
    results.forEach((r) => {
      const cls = r.correct === true ? 'ok' : (r.correct === false ? 'no' : 'unk');
      const item = el('div', `cet-q-result cet-q-${cls}`);
      item.dataset.cetQuestionId = r.questionId || '';
      const letters = 'ABCDE';
      const pick = r.choice == null ? '未作答' : letters[r.choice];
      const right = r.answer == null ? '无参考答案' : `正确 ${letters[r.answer]}`;
      const verdict = r.correct === true ? '答对' : (r.correct === false ? '答错' : '不计分');
      item.appendChild(el('div', 'cet-q-stem',
        `第 ${r.number} 题　你选 ${pick} · ${right} · ${verdict}`));
      if (r.evidenceRef) {
        const ev = r.evidenceRef;
        const btn = el('button', 'cet-btn-sm cet-evidence-btn', '▶ 听原声证据');
        btn.title = `原声证据 ${fmt(ev.startSec)}–${fmt(ev.endSec)}`;
        btn.onclick = async () => {
          btn.disabled = true;
          btn.textContent = '播放中…';
          const result = await playSentenceByRange(ev.startSec, ev.endSec, {});
          btn.disabled = false;
          btn.textContent = '▶ 听原声证据';
          if (result !== 'ended') flashHint('原声证据播放被中断，可再点一次', true);
        };
        item.appendChild(btn);
      }
      list.appendChild(item);
    });
  }

  async function enterDrill() {
    if (S.practice && S.practice.status !== 'complete') {
      setStep('drill');
      return;
    }
    const ready = S.marks.find((m) => m.confirmedSentenceId && markState(m) !== 'done');
    if (!ready) {
      flashHint(S.marks.some((m) => m.confirmedSentenceId)
        ? '已确认的卡点都练完了，可进入今日复习'
        : '先在「看原文」阶段确认一个卡点句子，再进入精练', true);
      return;
    }
    await startPractice(ready);
  }

  // ── 带练 ────────────────────────────────────────────────
  async function startPractice(m, opts) {
    const attempt = opts || {};
    try {
      const pr = await api('POST', '/practices', { markId: m.markId });
      S.practice = pr;
      // 卡点修复 A-5（补充）：带练一建立，这个标记就被 cet_practices 占用了 ——
      // 后端此后必定拒绝删除（MARK_IN_PRACTICE）。本地 m.inPractice 还停在建立标记时的
      // false，必须立刻同步，否则要等下一次刷新列表才会禁用删除按钮。
      // 这里只改本地对象，不发请求：占用状态由「刚建的带练」唯一确定，不需要再问后端。
      m.inPractice = true;
      bindPlaybackEvents();
      save();          // F03：不存下来，刷新后就找不回带练
      setStep('drill', { force: true });
      autoPlayDrillOnce();
      const confirmed = S.marks.filter((mark) => mark.confirmedSentenceId);
      flashHint(`第 ${confirmed.findIndex((mark) => mark.markId === m.markId) + 1} / ${confirmed.length} 句`);
    } catch (err) {
      if (err.code === 'ATTEMPT_REQUIRED') {
        // K1 无题材料：静默补空 attempt 后重试一次，不把 backend 细节抛给用户。
        if (!attempt.retried && !S.questions.length && await ensureAttemptForNoQuestions()) {
          await startPractice(m, { retried: true });
          return;
        }
        flashHint('请先交卷，再开始带练', true);
      } else if (err.code === 'CONFIRMATION_REQUIRED') {
        flashHint('请先确认真正卡住的句子', true);
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
      renderExplainContent(container, data);
      return;
    }

    if (action === 'transfer' || data.noCandidate) {
      // F05：恢复带练时若停在换句步骤，同样要隐藏原文并恢复原速
      hideTextForTransfer();
      renderTransfer({ currentStep: step, transferStatus: S.practice.transferStatus }, container);
      return;
    }

    if (action === 'chunk') {
      renderChunkContent(container, data);
    }

    if (action === 'slow' || action === 'replay' || action === 'chunk') {
      // K6：drill 步骤显示原文；全局字幕不在这里直接动（统一由 syncTranscriptVisibility 管）。
      container.appendChild(el('div', 'cet-ai-note',
        `当前步骤：${ACTION_LABEL[action] || action}${step.speed && step.speed < 1 ? `（${step.speed}x）` : ''}`));
      return;
    }

    if (action === 'finish') {
      container.appendChild(el('div', 'cet-ai-note', '本轮已结束，可保存或开始复习。'));
    }
  }

  // K7/C5：解释步骤。兼容包 D 的离线兜底 payload：
  // {mode:'offline', words:[{word,ipa,meaning}], translation?, hint}
  function renderExplainContent(container, data) {
    const text = String((data && data.explanation) || '').trim();
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
    // 包 D（D2）契约：无 key 的 explain 返回 200，data.mode='offline'，
    // words 每项 {word, ipa, meaning}，可选 translation，末尾 hint 为中性文案。
    if (data && (data.mode === 'offline' || Array.isArray(data.words))) {
      const box = el('div', 'cet-offline-explain');
      box.setAttribute('data-cet-offline', '1');
      if (data.translation) box.appendChild(el('div', 'cet-offline-translation', String(data.translation)));
      const simpleWords = new Set(['i', 'we', 'you', 'he', 'she', 'it', 'they', 'me', 'us', 'him', 'her', 'them', 'my', 'our', 'your', 'his', 'its', 'their', 'a', 'an', 'the', 'am', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'do', 'does', 'did', 'have', 'has', 'had', 'and', 'or', 'but', 'of', 'to', 'in', 'on', 'at', 'for', 'with', 'from', 'by', 'as', 'this', 'that', 'these', 'those']);
      const words = (Array.isArray(data.words) ? data.words : []).filter((item) =>
        !simpleWords.has(String(typeof item === 'string' ? item : item.word || '').toLowerCase())).slice(0, 8);
      if (words.length) {
        const gl = el('div', 'cet-offline-words');
        words.forEach((item) => {
          const word = typeof item === 'string' ? item : (item.word || '');
          const meaning = typeof item === 'object' ? (item.meaning || '') : '';
          const ipa = typeof item === 'object' ? (item.ipa || '') : '';
          if (!word) return;
          const row = el('div', 'cet-offline-word');
          row.appendChild(el('span', 'cet-offline-word-text', word));
          if (ipa) row.appendChild(el('span', 'cet-offline-word-ipa', `/${ipa}/`));
          if (meaning) row.appendChild(el('span', 'cet-offline-word-meaning', meaning));
          gl.appendChild(row);
        });
        box.appendChild(gl);
      }
      if (data.hint) box.appendChild(el('div', 'cet-ai-note', String(data.hint)));
      container.appendChild(box);
      return;
    }
    const sentence = practiceSentence();
    const note = el('div', 'cet-ai-note',
      sentence && sentence.text ? `暂无智能解释。原句：${sentence.text}` : '暂无可用解释，可继续回听或保存本轮。');
    container.appendChild(note);
  }

  function renderChunkContent(container, data) {
    const chunks = data && Array.isArray(data.chunks) ? data.chunks : [];
    if (!chunks.length) return;
    const box = el('div', 'cet-chunks');
    chunks.forEach((chunk) => {
      const btn = el('button', 'cet-btn-sm cet-chunk', chunk.text || '');
      btn.onclick = () => {
        const sent = practiceSentence();
        if (sent) playSentenceBySentence(sent, { rate: 0.75 });
      };
      box.appendChild(btn);
    });
    container.appendChild(box);
  }

  function practiceSentence() {
    if (!S.practice) return null;
    const sent = S.practice.sentence;
    if (sent && typeof sent === 'object') return sent;
    return findSentence(S.practice.sentenceId || S.practice.sentence);
  }

  function lookupWordFromDrill(anchor, word) {
    const text = String(word || '').trim();
    if (!text) return;
    const api = window.__ws || {};
    const fn = typeof api.lookupWord === 'function'
      ? api.lookupWord
      : (typeof api.showWordMeaning === 'function' ? api.showWordMeaning : null);
    if (fn) {
      try {
        // 约定签名：__ws.lookupWord(anchorElement, word)；W 未提供时回退到工作区全局函数。
        fn(anchor, text);
        return;
      } catch (err) {
        console.warn('[cet] 点词查询接口调用失败', err);
      }
    }
    if (typeof window.showWordMeaning === 'function') {
      try {
        window.showWordMeaning({ currentTarget: anchor, stopPropagation() {} });
        return;
      } catch (err) {
        console.warn('[cet] showWordMeaning 回退失败', err);
      }
    }
    flashHint('点词查询暂不可用，请刷新页面后重试', true);
  }

  function renderClickableSentence(container, sentence) {
    const text = String((sentence && sentence.text) || '');
    const parts = text.split(/([A-Za-z][A-Za-z'’\-]*)/g);
    parts.forEach((part) => {
      if (!part) return;
      if (/^[A-Za-z]/.test(part)) {
        const wordBtn = el('button', 'cet-drill-word', part);
        wordBtn.type = 'button';
        wordBtn.dataset.word = part.toLowerCase();
        wordBtn.dataset.query = part;
        wordBtn.onclick = (event) => {
          // 工作区有全局 click 监听会关闭 #word-popover，K 的按钮不是
          // .subtitle-word，必须阻止冒泡，否则词卡刚打开就被关掉。
          event.stopPropagation();
          event.preventDefault();
          lookupWordFromDrill(wordBtn, part);
        };
        container.appendChild(wordBtn);
      } else {
        container.appendChild(document.createTextNode(part));
      }
    });
  }

  function playCurrentPracticeSentence() {
    const sent = practiceSentence();
    if (!sent) return Promise.resolve('interrupted');
    return playSentenceBySentence(sent, {});
  }

  function autoPlayDrillOnce() {
    if (!S.practice || S.practice.status === 'complete') return;
    const key = `${S.practice.practiceId || 'practice'}:${S.practice.currentStepId || 'initial'}`;
    if (S.drillAutoPlayKey === key) return;
    S.drillAutoPlayKey = key;
    playCurrentPracticeSentence();
  }

  function renderPracticeComplete(body) {
    const sentence = practiceSentence();
    const practiceId = S.practice && S.practice.practiceId;
    const ask = S.favoriteAskSentence && S.favoriteAskedPracticeId !== practiceId;
    if (ask) {
      body.appendChild(el('div', 'cet-drill-complete-title', '本轮已保存。'));
      const askBox = el('div', 'cet-favorite-ask');
      askBox.appendChild(el('div', 'cet-favorite-ask-text', '把这句收藏到「我的收藏」？'));
      if (sentence && sentence.text) {
        askBox.appendChild(el('div', 'cet-favorite-ask-sentence', sentence.text));
      }
      const row = el('div', 'cet-favorite-ask-actions');
      const yes = el('button', 'cet-btn', '收藏这句');
      yes.id = 'cet-favorite-yes';
      yes.onclick = saveFavoriteSentence;
      const no = el('button', 'cet-btn-sm', '不用了');
      no.id = 'cet-favorite-no';
      no.onclick = declineFavoriteSentence;
      row.appendChild(yes);
      row.appendChild(no);
      askBox.appendChild(row);
      body.appendChild(askBox);
      return;
    }
    body.appendChild(el('div', 'cet-drill-complete-title', '本轮已保存。'));
    body.appendChild(el('div', 'cet-ai-note', '可以点「今日复习」继续，或回到卡点列表看下一处。'));
    const back = el('button', 'cet-btn-sm cet-inline-link', '← 回到看原文');
    back.onclick = () => setStep('review');
    body.appendChild(back);
  }

  // K6：带练面板（通过 __ws.mountStepPanel('drill', el) 放进右栏「本步骤」）。
  function renderPractice() {
    const head = $('#cet-drill-head');
    const body = $('#cet-drill-body');
    if (!head || !body) return;
    head.innerHTML = '';
    head.appendChild(el('strong', null, '卡点带练'));
    body.innerHTML = '';
    if (!S.practice) {
      body.appendChild(el('div', 'cet-step-lead', '还没有开始带练。先在「看原文」阶段确认一个卡点句子。'));
      const btn = el('button', 'cet-btn-sm cet-inline-link', '去看原文 →');
      btn.onclick = () => setStep('review');
      body.appendChild(btn);
      return;
    }
    if (S.practice.status === 'complete') {
      renderPracticeComplete(body);
      return;
    }

    const sentence = practiceSentence();
    const card = el('div', 'cet-drill-card');
    const confirmed = S.marks.filter((m) => m.confirmedSentenceId);
    const currentIndex = confirmed.findIndex((m) => m.markId === S.practice.markId);
    card.appendChild(el('div', 'cet-drill-kicker', `正在练的句子 · 第 ${currentIndex + 1} / ${confirmed.length} 句`));
    const sentenceEl = el('div', 'cet-drill-sentence');
    sentenceEl.id = 'cet-drill-sentence';
    if (S.practice.currentStep && S.practice.currentStep.action === 'transfer') {
      sentenceEl.textContent = '原文已遮住，请先完成换句检验。';
    } else {
      renderClickableSentence(sentenceEl, sentence);
    }
    card.appendChild(sentenceEl);
    const play = el('button', 'cet-btn-sm cet-drill-play', '▶ 播放');
    play.id = 'cet-drill-play';
    play.onclick = () => playCurrentPracticeSentence();
    card.appendChild(play);
    body.appendChild(card);

    // K6：原句回放 / 慢放 / 分块回放全部走 __ws.playSentence；不直接改 audio.playbackRate。
    const quick = el('div', 'cet-drill-quick');
    [['replay', '原句回放'], ['slow', '慢放'], ['chunk', '分块回放']].forEach(([action, label]) => {
      const btn = el('button', 'cet-btn-sm', label);
      btn.dataset.cetDrillAction = action;
      btn.onclick = () => localPracticeAction(action);
      quick.appendChild(btn);
    });
    body.appendChild(quick);

    const fbRow = el('div', 'cet-feedback');
    [['sound', '没听清'], ['meaning', '不懂意思'], ['heard', '听懂了']].forEach(([fb, label]) => {
      const b = el('button', 'cet-btn', label);
      b.dataset.cetFeedback = fb;
      b.onclick = () => sendFeedback(fb, label);
      fbRow.appendChild(b);
    });
    body.appendChild(fbRow);

    const out = el('div', 'cet-practice-out');
    out.id = 'cet-practice-out';
    body.appendChild(out);
    renderCurrentStepInto(out);

    const hasNext = S.marks.some((m) => m.confirmedSentenceId
      && m.markId !== S.practice.markId && markState(m) !== 'done');
    const saveBtn = el('button', 'cet-btn', hasNext ? '完成本句，继续下一句' : '保存本轮');
    saveBtn.id = 'cet-save-practice';
    saveBtn.onclick = finishPractice;
    body.appendChild(saveBtn);
  }

  function renderDrillPanel() {
    renderPractice();
  }

  async function localPracticeAction(action) {
    if (!S.practice || !S.practice.practiceId) return;
    const practiceId = S.practice.practiceId;
    try {
      const r = await api('POST', `/practices/${encodeURIComponent(practiceId)}/local`, {
        action,
        actionId: uid(`local-${action}`),
        stepId: S.practice.currentStepId,
        materialVersion: S.materialVersion,
      });
      if (!S.practice || S.practice.practiceId !== practiceId) return;
      S.practice = r && r.practiceId ? r : mergePracticeState(S.practice, r);
      save();
      renderPractice();
      executeLocalAction(action, (S.practice && S.practice.currentStep) || {});
    } catch (err) {
      flashHint(`本地回放失败：${err.message}`, true);
    }
  }

  async function saveFavoriteSentence() {
    const sentence = practiceSentence();
    const lessonId = (window.__ws && window.__ws.lessonId)
      || (location.pathname.match(/\/workspace\/(\d+)/) || [])[1];
    if (!sentence || !lessonId) { flashHint('收藏失败：缺少句子或课程信息', true); return; }
    const segmentIndex = Array.isArray(sentence.segment_ids) && sentence.segment_ids.length
      ? sentence.segment_ids[0]
      : (sentence.index != null ? sentence.index : 0);
    try {
      const resp = await fetch(`/api/v2/lessons/${encodeURIComponent(lessonId)}/phase-b`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          segment_index: Number(segmentIndex) || 0,
          start_seconds: sentenceStart(sentence),
          end_seconds: sentenceEnd(sentence),
          text: sentence.text || '',
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      S.favoriteAskedPracticeId = S.practice && S.practice.practiceId;
      S.favoriteAskSentence = null;
      save();
      renderPractice();
      flashHint('已收藏到「我的收藏」');
    } catch (err) {
      flashHint(`收藏失败：${err.message}`, true);
    }
  }

  function declineFavoriteSentence() {
    S.favoriteAskedPracticeId = S.practice && S.practice.practiceId;
    S.favoriteAskSentence = null;
    save();
    renderPractice();
  }

  // 动作 → 中文说明（计划 §5：两类反馈要给合理不同的帮助）
  const ACTION_LABEL = {
    replay: '原句回放',
    slow: '慢放',
    chunk: '短语分段',
    explain: '解释句意',
    transfer: '换句检验',
    finish: '本句完成',
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
        // 非换句步骤：三步流程按当前步骤统一决定原文显隐（drill 显示原文）。
        syncTranscriptVisibility();
      }
      save();
      if (action === 'finish') queueNextPractice();
    } catch (err) {
      clearTimeout(timer);
      if (waitingNote.parentNode) waitingNote.remove();
      // S6：失败同样要过滤（身份 + 序号）
      if (!S.practice || S.practice.practiceId !== practiceAtSend) return;
      if (seq !== feedbackSeq) return;

      // 后端 503 的 detail.practice 里带了已落库的最新状态，先合并再决定兜底。
      const detail = err && err.payload && err.payload.detail;
      const state = detail && (detail.practice || (detail.data && detail.data.practice));
      if (state && state.practiceId === practiceAtSend) {
        const merged = mergePracticeState(S.practice, state);
        if (merged) S.practice = merged;
      }

      // K7：听懂了不能卡在「模型尚未配置」；无 key 时直接走本地结束。
      if (feedback === 'heard' && (err.code === 'AI_NOT_CONFIGURED' || err.status === 503)) {
        if (out) {
          out.className = 'cet-practice-out ok';
          out.textContent = '当前环境未启用智能讲解，本轮直接完成。';
        }
        flashHint('已跳过智能讲解，保存本轮');
        await finishPractice({ offline: true });
        return;
      }

      // K7/C5：不懂意思时展示包 D 的离线兜底；D 未就绪时退化为中性原句提示。
      if (feedback === 'meaning' && (err.code === 'AI_NOT_CONFIGURED' || err.status === 503)) {
        if (out) {
          out.className = 'cet-practice-out';
          out.innerHTML = '';
          const sentence = practiceSentence();
          renderExplainContent(out, sentence && sentence.text
            ? { mode: 'offline', words: [], hint: '当前环境未启用智能讲解，先看原句：' + sentence.text }
            : { mode: 'offline', words: [], hint: '当前环境未启用智能讲解，可以先回听或保存本轮。' });
        }
        flashHint('已显示离线释义兜底');
        return;
      }

      if (out) {
        out.className = 'cet-practice-out error';
        if (err.code === 'STALE_RESPONSE') {
          // N2：冲突后主动拉取权威状态，让用户能继续，而不是要求刷新
          out.textContent = '这一步已更新，正在同步…';
          await resyncPractice();
          out.textContent = '已同步到最新步骤，可以继续';
        } else {
          out.textContent = `反馈失败：${err.message}`;
        }
      }
    }
  }

  // F01：把后端生成正文渲染出来（cet_service 放在 currentStep.data 里）
  // K7：包 D 的离线兜底 payload 也是 currentStep.data，复用同一个渲染器。
  function renderExplain(resp, step) {
    const out = $('#cet-practice-out');
    if (!out) return;
    const data = (step && step.data) || (resp && resp.currentStep && resp.currentStep.data) || {};
    renderExplainContent(out, data);
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
    // K6：原句播放的倍速统一经 __ws.setRate（由 W 同步播放器与倍速下拉）。
    const setRate = wsFn('setRate');
    if (setRate) {
      try { setRate(1); } catch (_) { /* 原页面异常不阻塞 */ }
    } else {
      // 兜底也不直接写 audio.playbackRate：走工作区下拉的既有处理函数。
      const sel = document.getElementById('playback-rate');
      if (sel && sel.value !== '1') {
        sel.value = '1';
        if (typeof window.onPlaybackRateChange === 'function') {
          try { window.onPlaybackRateChange(); } catch (_) { /* 原页面异常不阻塞 */ }
        }
      }
    }
    // 换句候选使用独立 audio，不是主播放器；这里单独恢复不影响 K6 的原句原语。
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
      const offline = data.reason === 'offline' || data.source === 'offline';
      box.appendChild(el('div', 'cet-transfer-stem', offline ? '换句检验（本地完成）' : '换句检验'));
      const note = el('div', 'cet-ai-note',
        offline
          ? '当前环境未启用智能换句检验，本轮按本地完成处理。'
          : '当前没有可用的换句材料，本轮按本地完成处理。');
      note.setAttribute('data-cet-unverified', '1');
      box.appendChild(note);
      if (!offline) {
        const tip = el('div', 'cet-ai-note',
          '你仍可以回听原句、慢放或保存本轮。');
        box.appendChild(tip);
      }
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
      queueNextPractice();
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

  // 执行本地动作：原句回放 / 慢放 / 分块回放统一走 __ws.playSentence。
  // K6 明确禁止这里再直接改 audio.playbackRate / currentTime。
  function executeLocalAction(action, step) {
    const sent = practiceSentence();
    if (!sent) return;
    const rate = action === 'slow' || action === 'chunk' ? 0.75 : 1;
    playSentenceBySentence(sent, { rate });
  }

  // 旧接口保留给换句流程/自动化测试兼容；三步流程统一走 __ws.setTranscriptHidden。
  function applyTextVisibility(visible) {
    setTranscriptHiddenCompat(visible === false);
  }

  // 兼容旧 W 未接入时的本地隐藏逻辑（仅 fallback）。
  // F05：隐藏/恢复「含原文」的区域。
  // 只控显隐（加 class），不改原 DOM 结构 —— 原 UI 完整保留。
  // 覆盖三处泄露原文的地方：
  //   1) #current-subtitle / #subtitle-translation  （当前句字幕）
  //   2) #subtitle-timeline 的 outline-preview      （内容大纲里的句子摘要）
  //   3) #reading-passage                            （整篇阅读正文）
  function legacyApplyTextVisibility(visible) {
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
    return currentTranscriptHidden();
  }

  // F05：「听懂了」→ 换句流程。隐藏提示、恢复 1.0x 原速。
  function hideTextForTransfer() {
    // 三步流程统一走 __ws.setTranscriptHidden；旧 applyTextVisibility 仅保留兼容导出。
    setTranscriptHiddenCompat(true);
    const sentenceEl = $('#cet-drill-sentence');
    if (sentenceEl) sentenceEl.textContent = '原文已遮住，请先完成换句检验。';
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
    const note = el('div', 'cet-ai-note', '正在准备解释…（稍后可继续回听或保存本轮）');
    out.appendChild(note);
    setTimeout(() => {
      if (note.parentNode) {
        note.textContent = '暂时没有更多解释；你仍可以回听、慢放、看原文或保存本轮。';
        note.classList.add('warn');
      }
    }, 3000);
  }

  function queueNextPractice() {
    if (!S.practice || !S.marks.some((m) => m.confirmedSentenceId
      && m.markId !== S.practice.markId && markState(m) !== 'done')) return;
    clearTimeout(S.advanceTimer);
    const practiceId = S.practice.practiceId;
    S.advanceTimer = setTimeout(() => {
      if (S.practice && S.practice.practiceId === practiceId && S.practice.status !== 'complete') {
        finishPractice();
      }
    }, 1200);
  }

  async function finishPractice(opts) {
    if (!S.practice) return;
    clearTimeout(S.advanceTimer);
    const options = opts || {};
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
      if (done) {
        if (!S.completedMarkIds.includes(S.practice.markId)) S.completedMarkIds.push(S.practice.markId);
        const next = S.marks.find((m) => m.confirmedSentenceId && markState(m) !== 'done');
        save();
        if (next) {
          stopSentencePlayback();
          await startPractice(next);
          return;
        }
        // K9：保存成功后只问一次是否收藏到「我的收藏」。
        const practiceId = S.practice.practiceId;
        if (S.favoriteAskedPracticeId !== practiceId) {
          S.favoriteAskSentence = practiceSentence();
        }
        flashHint(options.offline ? '本轮已完成' : '本轮已保存，已加入今日复习');
      } else {
        flashHint('本轮已保存');
      }
      renderMarks();
      renderPractice();     // 按完成态重绘：停用反馈入口
      renderStepActions();
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
  function replayRange(startSec, endSec, opts) {
    return playSentenceByRange(startSec, endSec, opts || {});
  }

  // ── 今日复习（K10）────────────────────────────────────
  let reviewState = null;

  function reviewSentence(entry) {
    return (entry && entry.item && entry.item.sentence)
      || (entry && entry.sentence)
      || null;
  }

  function reviewMaterialMatches(entry) {
    const sent = reviewSentence(entry);
    const entryMaterial = (entry && entry.item && entry.item.materialId)
      || (sent && sent.materialId) || null;
    if (!entryMaterial || !S.materialId) return true;
    return entryMaterial === S.materialId
      || entryMaterial === S.materialId.replace(/^cet[46]-/, '');
  }

  function playReviewEntry(entry) {
    if (!reviewMaterialMatches(entry)) {
      flashHint('该复习条目属于其他材料，请先打开对应课程再回听', true);
      return Promise.resolve('interrupted');
    }
    const sent = reviewSentence(entry);
    if (!sent) return Promise.resolve('interrupted');
    return playSentenceBySentence(sent, {});
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
      S.reviewSessionActive = true;
      S.reviewNotice = '';
      S.reviewReturnStep = S.step && S.step !== 'review' ? S.step : (S.reviewReturnStep || 'blind');
      S.reviewEntryKey = '';
      S.reviewRevealed = false;
      setStep('review', { force: true });
      save();
      flashHint('复习已开始（最多三处）');
    } catch (err) {
      if (err.code === 'NO_REVIEW_ITEMS') {
        S.reviewNotice = '暂时没有待复习的卡点';
        renderReviewPanel();
        renderStepActions();
        flashHint(S.reviewNotice, true);
      } else {
        flashHint(`开始复习失败：${err.message}`, true);
      }
    }
  }

  // 兼容旧调用：复习内容现在渲染在 review 步骤面板里。
  function renderReview() {
    renderReviewPanel();
  }

  function renderReviewSession(body) {
    if (!reviewState) {
      body.appendChild(el('div', 'cet-empty', '复习尚未开始'));
      return;
    }
    const entries = reviewState.entries || [];
    const cur = entries.find((e) => e.status === 'current')
      || entries.find((e) => e.status === 'pending')
      || null;
    const mins = Math.round((reviewState.remainingMs || 0) / 60000);
    const phase = reviewState.status === 'paused' ? '已暂停'
      : (reviewState.status === 'complete' ? '已结束' : '进行中');
    body.appendChild(el('div', 'cet-review-status',
      `${phase} · 剩余约 ${mins} 分钟${cur ? ` · 第 ${(cur.position || 0) + 1} 处 / 共 ${entries.length} 处` : ''}`));

    if (reviewState.status === 'complete' || !cur) {
      body.appendChild(el('div', 'cet-review-complete', '本次复习已完成。'));
      const back = el('button', 'cet-btn-sm cet-inline-link', '返回学习');
      back.onclick = exitReviewSession;
      body.appendChild(back);
      return;
    }

    const key = String(cur.entryId || cur.id || cur.position);
    if (S.reviewEntryKey !== key) {
      S.reviewEntryKey = key;
      S.reviewRevealed = false;
      syncTranscriptVisibility();
      // 进入一处复习先盲听：自动播放一次。暂停态不自动播。
      if (reviewState.status === 'active') {
        setTimeout(() => { if (S.reviewSessionActive && S.reviewEntryKey === key) playReviewEntry(cur); }, 30);
      }
    }

    const card = el('div', 'cet-review-card');
    card.appendChild(el('div', 'cet-review-kicker', '先盲听，再决定是否看原文'));
    if (!S.reviewRevealed) {
      card.appendChild(el('div', 'cet-review-hidden', '原文已遮住。'));
      const play = el('button', 'cet-btn-sm', '▶ 再听一遍');
      play.onclick = () => playReviewEntry(cur);
      card.appendChild(play);
      const show = el('button', 'cet-btn-sm cet-btn-confirm', '显示原文');
      show.id = 'cet-review-reveal';
      show.onclick = () => {
        S.reviewRevealed = true;
        syncTranscriptVisibility();
        renderReviewPanel();
      };
      card.appendChild(show);
    } else {
      const sent = reviewSentence(cur);
      card.appendChild(el('div', 'cet-review-sentence', (sent && sent.text) || '(无句子)'));
      const play = el('button', 'cet-btn-sm', '▶ 再听一遍');
      play.onclick = () => playReviewEntry(cur);
      card.appendChild(play);
    }
    const actions = el('div', 'cet-review-actions');
    const done = el('button', 'cet-btn-sm', '这处练完了');
    done.onclick = () => reviewAction('complete', cur.practiceId);
    const skip = el('button', 'cet-btn-sm', '跳过');
    skip.onclick = () => reviewAction('skip', cur.practiceId);
    actions.appendChild(done);
    actions.appendChild(skip);
    card.appendChild(actions);
    body.appendChild(card);

    // 全部条目进度（只显示编号/状态，不泄露原文）
    const list = el('div', 'cet-review-list');
    entries.forEach((entry, i) => {
      const label = { current: '进行中', pending: '待复习', done: '已完成', skipped: '已跳过' }[entry.status] || entry.status;
      const row = el('div', `cet-review-row cet-review-${entry.status}`);
      row.appendChild(el('span', 'cet-review-idx', `${i + 1}`));
      row.appendChild(el('span', 'cet-review-label', label));
      list.appendChild(row);
    });
    body.appendChild(list);

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

  function exitReviewSession() {
    S.reviewSessionActive = false;
    S.reviewRevealed = false;
    S.reviewEntryKey = '';
    const back = S.reviewReturnStep || 'blind';
    setStep(back === 'review' ? 'blind' : back, { force: true });
  }

  async function reviewAction(action, practiceId) {
    if (!reviewState) return;
    // R4：暂停 / 跳过 / 结束都要先停掉正在播的回放，
    // 否则页面显示「已暂停」但音频还在响。
    if (action === 'pause' || action === 'skip' || action === 'complete' || action === 'finish') {
      stopSentencePlayback();
      stopActiveReplay();
    }
    try {
      // 后端要求 actionId 用于幂等去重（重复提交不重复计时）
      const payload = { action, actionId: uid(`rv-${action}`) };
      if (practiceId) payload.practiceId = practiceId;
      const r = await api('POST', `/reviews/${reviewState.id}/actions`, payload);
      reviewState = { ...reviewState, ...r };
      renderReviewPanel();
      renderMarks();
      renderStepActions();
      save();
      if (reviewState.status === 'complete') {
        flashHint('复习已完成');
      }
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
        // K1：步骤按材料存在现有的 cet.state.v2.<materialId>，刷新恢复同一步。
        step: S.step,
        attemptId: S.attempt && S.attempt.attemptId,
        practiceId: S.practice && S.practice.practiceId,
        completedMarkIds: S.completedMarkIds,
        reviewId: reviewState && (reviewState.reviewId || reviewState.id),
        reviewReturnStep: S.reviewReturnStep || null,
        favoriteAskedPracticeId: S.favoriteAskedPracticeId || null,
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
    if (!S.materialId) return {};
    const saved = loadSaved();
    S.completedMarkIds = Array.isArray(saved.completedMarkIds) ? saved.completedMarkIds : [];
    S.favoriteAskedPracticeId = saved.favoriteAskedPracticeId || null;
    try {
      const r = await api('GET', `/marks?materialId=${encodeURIComponent(S.materialId)}`
        + `&listeningSessionId=${encodeURIComponent(sessionId())}`);
      const items = (r && (r.items || r.marks)) || [];
      if (items.length) {
        S.marks = items;
        flashHint(`已恢复 ${items.length} 个卡点标记`);
      }
      ensureTimeline();
      renderTimelineMarks();
    } catch (_) { /* 恢复失败不阻塞主流程 */ }

    // F03：恢复交卷结果（原来只恢复标记，刷新后右侧回到「未交卷」）
    try {
      if (saved.attemptId) {
        const at = await api('GET', `/attempts/${saved.attemptId}`);
        // R1：归属核对——取回的记录必须属于当前材料，否则丢弃（防跨材料串入）
        if (at && at.attemptId && at.materialId === S.materialId) {
          S.attempt = at;
        }
      }
    } catch (_) { /* 交卷恢复失败不阻塞 */ }

    // F03：恢复带练（原来只恢复标记，刷新后带练面板消失）
    try {
      if (saved.practiceId) {
        const pr = await api('GET', `/practices/${saved.practiceId}`);
        // R1：练习也必须属于当前材料
        if (pr && pr.practiceId && pr.materialId === S.materialId) {
          S.practice = pr;
          bindPlaybackEvents();
        }
      }
    } catch (_) { /* 带练恢复失败不阻塞 */ }

    // 恢复复习会话（计划 T11：重启后恢复原会话和步骤）
    try {
      if (saved.reviewId) {
        const rv = await api('GET', `/reviews/${saved.reviewId}`);
        if (rv && rv.status && rv.status !== 'complete') {
          reviewState = rv;
          S.reviewSessionActive = true;
          S.reviewReturnStep = saved.reviewReturnStep || 'blind';
        }
      }
    } catch (_) { /* 复习恢复失败不阻塞 */ }
    return saved;
  }

  // ── 初始化 ──────────────────────────────────────────────
  async function boot() {
    // 通用模式（ELT_CET_MODE=0）回归：卡点融合三步 UI 是 CET 产品能力，
    // 不在通用模式挂载，保持原版工作区行为。
    if (window.__ws && window.__ws.cetOn === false) {
      console.info('[cet] 通用模式：不挂载卡点融合三步 UI');
      return;
    }
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

      // 卡点修复 A-3：**材料是否适用**这一层。
      // 与模板里的 {% if cet_features.cet_listening_agent %} 是两层不同的事：
      //   - 模板那层管「模块是否加载」（功能开关 / 有没有接入）
      //   - 这里管「这份材料能不能用」（有没有逐句时间轴）
      // 无时间轴材料（sentenceCount=0，如只导了音频）原来照样挂出
      // 「开始卡点练习 / 今日复习」，点进去标记时才报「该材料没有可用句子时间轴」——
      // 实测：toolbar=true、能进入 listening 模式、标记才失败，就是假入口。
      // 现在直接不 mount，用户看到的仍是原页面（不多出点了没反应的按钮）。
      if (!S.sentences.length) {
        console.info(
          `[cet] 材料 ${S.materialId} 没有逐句时间轴（sentences=0），`
          + '不挂载卡点工具栏：标记功能依赖句子时间轴，挂出来也是点了报错的假入口。'
        );
        return;
      }

      const q = await api('GET', `/materials/${encodeURIComponent(S.materialId)}/questions`
        + `?version=${encodeURIComponent(S.materialVersion)}`);
      S.questions = (q && (q.questions || q.items)) || [];

      mount();
      renderQuestions();
      renderMarks();
      // F10：播放器元素由 workspace.html 在 lessonData 加载后才创建，
      // 这时可能还不存在。等它就绪再挂时间线（只挂一次）。
      waitForPlayerThenTimeline();

      // K1：恢复同一步；无历史时默认 blind（有交卷记录则进 review 看成绩）。
      const saved = await restore();
      let initial = STEP_KEYS.includes(saved.step) ? saved.step : (S.attempt ? 'review' : 'blind');
      if (S.reviewSessionActive) initial = 'review';
      if (initial === 'review' && S.questions.length && !S.attempt) initial = 'blind';
      if (initial === 'drill' && !S.practice) initial = (S.questions.length && !S.attempt) ? 'blind' : 'review';
      S.stepReady = true;
      // K5：init/restore 完成后必须补发首帧事件（setStep 的 force 会派发，
      // 不依赖 W 在 DOMContentLoaded 时读取 __cet.step 的兜底）。
      setStep(initial, { force: true });

      // H3：首页「今日复习」跳转 /workspace/<id>?review=1 时直接打开复习。
      // 只在本页生命周期自动触发一次；无待复习条目时停在 review 步骤并中性提示。
      const reviewWanted = new URLSearchParams(location.search).get('review') === '1';
      if (reviewWanted && !S.reviewQueryStarted) {
        S.reviewQueryStarted = true;
        if (initial !== 'review') S.reviewReturnStep = initial;
        setStep('review', { force: true });
        if (!S.reviewSessionActive) {
          setTimeout(() => {
            if (!S.reviewSessionActive) startReview();
          }, 0);
        }
      }
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

  // 标记不再绑键盘（用户决定），故不再注册 keydown 监听。

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
    // 卡点修复 A：标记列表重绘（自动化测试直接驱动，避免只能靠点按钮）
    renderMarks,
    // 卡点修复 A-1：标记后自动回听开关（默认关闭，状态持久化）
    setAutoplayOnMark,
    isAutoplayOnMark: () => S.autoplayOnMark,
    // K1/K2/K10：三步状态与面板（自动化/联调可直接读）
    setStep,
    renderBlindPanel,
    renderReviewPanel,
    renderDrillPanel,
    renderStepActions,
    get step() { return S.step; },
    // W5：W 的大纲按段显示卡点数，需要只读地拿到当前标记与材料身份。
    get marks() { return S.marks; },
    get materialId() { return S.materialId; },
  };
})();
