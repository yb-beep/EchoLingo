/* 四六级听力底座 · 播放适配层（cet-player.js）
 *
 * 职责（任务书 §4 P3 + §5 P5）：
 *   1. 学习位置恢复：POST/GET /api/v2/lessons/{id}/progress，刷新/重开都回到原位置
 *   2. 材料身份校验：进度与收藏都绑定 lesson id，切换材料不串数据
 *   3. 学习状态条：显示真实句数/进度，不编造掌握度或预测分数
 *   4. 快捷键归属（返修2 P2）：**队友卡点模块关闭时底座接管** Space/←/→/R/L，
 *      队友模块开启时全部让出；通用模式不注册。见下方「快捷键」小节。
 *   5. 幂等挂载：重复执行不会重复建 DOM、重复绑监听
 *
 * 不做：播放器本身（沿用 workspace.html 的 #local-media-player / ytPlayer）、
 *       字幕渲染、查词、AI 解释 —— 那些是原页面已有能力。
 *
 * 开关来源：window.__CET_BOOT__（= cet_mode.template_context()），不另写一套。
 */
(function () {
  'use strict';

  const BOOT = window.__CET_BOOT__ || {enabled: false, features: {}, copy: {}, lessonId: null};
  const CET_ON = Boolean(BOOT.enabled);
  const FEATURES = BOOT.features || {};
  const COPY = BOOT.copy || {};
  const LESSON_ID = Number(BOOT.lessonId || window.__ws?.lessonId || 0) || null;

  // 只负责「底座自己」的部分；CET 关闭时仍要保证进度恢复可用（原版回归）
  const CFG = {
    progressUrl: LESSON_ID ? `/api/v2/lessons/${LESSON_ID}/progress` : '',
    // 位置写入节流：播放中每 5s 最多一次，暂停/离开时立即补写
    saveIntervalMs: 5000,
    // 恢复位置的最小阈值：太靠开头不弹「已恢复」提示
    resumeMinSeconds: 3,
    // 播放器出现的最长等待（workspace.html 在 status 回来后才建播放器）
    playerWaitMs: 20000,
    playerPollMs: 200,
  };

  const S = {
    mounted: false,
    barEl: null,
    resumeNoteEl: null,
    lastSavedAt: 0,
    lastSavedSeconds: -1,
    pendingSeconds: null,
    restoring: false,
    restored: false,
    // 用户是否已经主动操作过播放器（seek/play/拖动）。一旦为 true，
    // 异步回来的进度就**不能**再覆盖用户当前位置 —— 否则「刚拖动就被弹回去」。
    userInteracted: false,
    saveTimer: null,
    playerWatchTimer: null,
    listenersBound: false,
    lessonVerified: false,
    saveInFlight: false,
    // ── 快捷键（返修2 P2）──
    // keydownBound：keydown 只绑一次的幂等标记（P2-4）
    keydownBound: false,
    // keyHandled：底座快捷键**实际处理过**的按键计数。队友开启态必须恒为 0（P2-2）
    keyHandled: 0,
    // 最近一次被处理的键（调试/断言用）
    lastKey: '',
    // 句循环开关（L 切换）；只影响底座自己，不写任何持久化
    loopEnabled: false,
    // 句循环锚定的那一句（{start,end,index}）。跨句尾时用它回跳，
    // 不用 activeSentenceIndex —— 越过句尾那一刻它已经指向下一句了。
    loopAnchor: null,
    // 句循环回跳后短暂静音，避免 timeupdate 连发导致反复 seek（抖动）
    loopGuardUntil: 0,
    // 当前是否**真实**接管了播放快捷键（P2-2 契约的三态核心，不是常量）
    ownsSpaceKey: false,
    // 句循环的 rAF 句柄（null = 未在跑）
    loopRaf: null,
    // R 重听的落点 {index, start}（调试与精确断言用）
    lastReplay: null,
  };

  const $ = sel => document.querySelector(sel);
  const $$ = sel => [...document.querySelectorAll(sel)];

  function fmt(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function log(...args) {
    if (window.__CET_DEBUG__) console.info('[cet-player]', ...args);
  }

  // ── 播放器访问（唯一来源：window.__ws，由 workspace.html 提供）──────
  function ws() { return window.__ws || null; }
  function audioEl() {
    const api = ws();
    if (api && typeof api.audio !== 'undefined') return api.audio;
    return document.getElementById('local-media-player');
  }
  function readTime() {
    const api = ws();
    if (api && typeof api.currentTime === 'number') return api.currentTime;
    const el = audioEl();
    if (el) return Number(el.currentTime || 0);
    return null;
  }
  function isPaused() {
    const el = audioEl();
    if (el) return Boolean(el.paused);
    return true;
  }
  function hasPlayer() {
    return Boolean(audioEl() || (ws() && ws().player));
  }

  // ── 学习状态条（不编造数据）─────────────────────────────────
  function buildBar() {
    if (S.barEl && S.barEl.isConnected) return S.barEl;
    const host = $('.learning-controls');
    if (!host) return null;

    const bar = document.createElement('div');
    bar.className = 'cet-study-bar';
    bar.id = 'cet-study-bar';
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', '学习状态');
    bar.innerHTML = `
      <span class="cet-study-item" data-cet-item="position">位置 <strong id="cet-position">—</strong> / <span id="cet-duration">—</span></span>
      <span class="cet-study-item" data-cet-item="sentence">当前句 <strong id="cet-sentence-index">—</strong></span>
      <span class="cet-study-item cet-muted" data-cet-item="saved">本课收藏 <strong id="cet-saved-count">—</strong></span>
      <span class="cet-study-item cet-muted" data-cet-item="note" id="cet-state-note"></span>
      <span class="cet-study-item" data-cet-item="loop" id="cet-loop-indicator" hidden>本句循环 <strong>开</strong></span>
      <span class="cet-study-actions">
        <button type="button" id="cet-restart-btn" title="回到本材料开头">从头开始</button>
      </span>`;

    // 放在播放控制条之后，属于同一视觉区块，不新开一栏
    host.insertAdjacentElement('afterend', bar);
    S.barEl = bar;

    bar.querySelector('#cet-restart-btn')?.addEventListener('click', () => {
      const api = ws();
      if (api && typeof api.seek === 'function') api.seek(0);
      markSaved(0, true);
      updateBar();
    });
    buildHotkeyHint(bar);
    return bar;
  }

  function updateBar() {
    // 归属先同步：队友模块可能晚于底座挂载（它的 boot() 要先等两个接口），
    // 这里顺手重判一次，保证 ownsSpaceKey 与「实际是否注册」永远一致。
    syncHotkeyOwnership();
    if (!S.barEl) return;
    const api = ws();
    const time = readTime();
    const duration = api && typeof api.duration === 'number' ? api.duration : 0;
    const posEl = S.barEl.querySelector('#cet-position');
    const durEl = S.barEl.querySelector('#cet-duration');
    const sentEl = S.barEl.querySelector('#cet-sentence-index');
    const noteEl = S.barEl.querySelector('#cet-state-note');
    const savedEl = S.barEl.querySelector('#cet-saved-count');

    if (posEl) posEl.textContent = time == null ? '—' : fmt(time);
    if (durEl) durEl.textContent = duration > 0 ? fmt(duration) : '—';

    if (sentEl) {
      const idx = api && typeof api.activeSentenceIndex === 'number' ? api.activeSentenceIndex : -1;
      const total = api && Array.isArray(api.sentences) ? api.sentences.length : 0;
      sentEl.textContent = idx >= 0 && total ? `${idx + 1} / ${total}` : '—';
    }

    if (noteEl) {
      const hasSentences = api && Array.isArray(api.sentences) && api.sentences.length > 0;
      const mediaUrl = api ? api.mediaUrl : '';
      if (!mediaUrl) {
        noteEl.textContent = '这份材料没有可播放音频。';
      } else if (!hasSentences) {
        noteEl.textContent = '暂无逐句原文，可整段播放。';
      } else {
        noteEl.textContent = '';
      }
    }

    if (savedEl && savedEl.dataset.filled !== '1') {
      // 收藏数由 cet-favorites.js 回填；未就绪时显示 —，不猜 0
      savedEl.textContent = '—';
    }
  }

  function showResumeNote(seconds) {
    if (!S.barEl) return;
    S.resumeNoteEl?.remove();
    const note = document.createElement('span');
    note.className = 'cet-resume-note';
    note.id = 'cet-resume-note';
    note.textContent = `已恢复到 ${fmt(seconds)}`;
    const actions = S.barEl.querySelector('.cet-study-actions');
    if (actions) actions.insertAdjacentElement('beforebegin', note);
    else S.barEl.appendChild(note);
    S.resumeNoteEl = note;
    window.setTimeout(() => note.remove(), 6200);
  }

  // ── 进度：读 ────────────────────────────────────────────────
  async function fetchProgress() {
    if (!CFG.progressUrl) return null;
    try {
      const resp = await fetch(CFG.progressUrl, {cache: 'no-store'});
      if (!resp.ok) return null;
      const data = await resp.json().catch(() => ({}));
      // 归属校验：返回的 lesson_id 必须就是当前材料，否则丢弃（防串材料）
      if (data.lesson_id != null && Number(data.lesson_id) !== LESSON_ID) {
        log('进度归属不符，丢弃', data.lesson_id, LESSON_ID);
        return null;
      }
      return {
        seconds: Number(data.last_position_seconds || 0),
        segmentIndex: Number(data.last_segment_index || 0),
      };
    } catch (_) {
      return null;
    }
  }

  // ── 进度：写 ────────────────────────────────────────────────
  function markSaved(seconds, force) {
    S.lastSavedSeconds = Number(seconds) || 0;
    S.lastSavedAt = force ? 0 : Date.now();
  }

  async function saveProgress(seconds, segmentIndex, {force = false} = {}) {
    if (!CFG.progressUrl) return;
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return;
    if (!force && Math.abs(value - S.lastSavedSeconds) < 1) return;
    if (S.saveInFlight) {
      S.pendingSeconds = value;
      return;
    }
    S.saveInFlight = true;
    S.lastSavedAt = Date.now();
    S.lastSavedSeconds = value;
    try {
      await fetch(CFG.progressUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          last_position_seconds: Number(value.toFixed(3)),
          last_segment_index: Math.max(0, Number(segmentIndex) || 0),
        }),
        keepalive: true,
      });
    } catch (_) {
      // 写失败不阻塞播放；下次节流会重试
    } finally {
      S.saveInFlight = false;
      if (S.pendingSeconds != null) {
        const next = S.pendingSeconds;
        S.pendingSeconds = null;
        if (Math.abs(next - S.lastSavedSeconds) >= 1) saveProgress(next, segmentIndex);
      }
    }
  }

  function currentSegmentIndex() {
    const api = ws();
    return api && typeof api.activeSentenceIndex === 'number' ? Math.max(0, api.activeSentenceIndex) : 0;
  }

  // ── 恢复学习位置 ────────────────────────────────────────────
  async function restorePosition() {
    if (S.restored || S.restoring) return;
    if (!hasPlayer()) return;
    S.restoring = true;
    try {
      const progress = await fetchProgress();
      if (!progress) return;
      const el = audioEl();
      // 用户在等待期间已经 seek/播放/拖动：尊重用户操作，不弹回旧位置。
      // 这不只是体验问题——它会让「刚拖到 30s 又被恢复逻辑拉回 44s」。
      if (S.userInteracted) {
        log('用户已操作播放器，跳过自动恢复');
        return;
      }
      const alreadyMoved = el ? Number(el.currentTime || 0) > 1 : false;
      if (alreadyMoved) {
        log('播放位置已离开起点，跳过自动恢复');
        return;
      }
      const duration = Number(ws()?.duration || 0);
      let target = progress.seconds;
      if (!Number.isFinite(target) || target <= 0) {
        // 没有位置但有句索引：退回到该句开头
        const sentences = ws()?.sentences || [];
        const unit = sentences[progress.segmentIndex];
        if (unit && Number.isFinite(Number(unit.start))) target = Number(unit.start);
      }
      if (!Number.isFinite(target) || target <= 0) return;
      if (duration > 0 && target > duration - 0.5) {
        // 上次停在了结尾，重开时从头开始更合理
        target = 0;
      }
      const api = ws();
      if (api && typeof api.seek === 'function') api.seek(target);
      else if (el) el.currentTime = target;
      S.restored = true;
      markSaved(target, true);
      if (target >= CFG.resumeMinSeconds) showResumeNote(target);
      updateBar();
      log('已恢复位置', target);
    } finally {
      S.restoring = false;
    }
  }

  /** 标记「用户已主动操作」，并让自动恢复不再发生。 */
  function markUserInteracted() {
    S.userInteracted = true;
    S.restored = true;
  }

  /**
   * 包一层 seek/play：在元数据就绪之前设置 currentTime 不会触发 seeked，
   * 只靠事件判断会漏掉「用户抢在 metadata 之前跳转」的情况，导致异步回来的
   * 旧进度把用户位置覆盖掉。这里在入口处就标记，彻底消除这个竞态。
   */
  function wrapPlayerControls() {
    const api = ws();
    if (!api || api.__cetWrapped) return;
    api.__cetWrapped = true;
    if (typeof api.seek === 'function') {
      const originalSeek = api.seek;
      api.seek = seconds => {
        markUserInteracted();
        return originalSeek(seconds);
      };
    }
    if (typeof api.play === 'function') {
      const originalPlay = api.play;
      api.play = () => {
        markUserInteracted();
        return originalPlay();
      };
    }
    if (typeof api.jumpSentence === 'function') {
      const originalJump = api.jumpSentence;
      api.jumpSentence = delta => {
        markUserInteracted();
        return originalJump(delta);
      };
    }
  }

  // ── 事件绑定（幂等）────────────────────────────────────────
  function bindPlayerListeners() {
    const el = audioEl();
    if (!el || el.dataset.cetProgressBound === '1') return;
    el.dataset.cetProgressBound = '1';

    el.addEventListener('timeupdate', () => {
      updateBar();
      // P2-1 句循环：跨过当前句句尾就回到句首。放在 updateBar 之后，
      // 保证状态条显示的是回跳前的位置（否则数字会跳两下）。
      enforceLoop();
      if (S.restoring) return;
      const now = Date.now();
      if (now - S.lastSavedAt < CFG.saveIntervalMs) return;
      saveProgress(Number(el.currentTime || 0), currentSegmentIndex());
    });

    // 暂停/播放切换时立即写一次，保证「关掉页面再打开」位置准确
    el.addEventListener('pause', () => {
      if (S.restoring) return;
      saveProgress(Number(el.currentTime || 0), currentSegmentIndex(), {force: true});
    });
    el.addEventListener('play', () => {
      // 用户主动播放：此后再回来的进度不得覆盖当前位置
      markUserInteracted();
      updateBar();
    });
    el.addEventListener('loadedmetadata', () => {
      updateBar();
      restorePosition();
    });
    el.addEventListener('seeked', () => {
      // 用户拖动进度条 / 逐句跳转：同样算主动操作
      markUserInteracted();
      if (S.restoring) return;
      saveProgress(Number(el.currentTime || 0), currentSegmentIndex(), {force: true});
      updateBar();
    });

    if (el.readyState >= 1) restorePosition();
  }

  function bindPageListeners() {
    if (S.listenersBound) return;
    S.listenersBound = true;

    // 关页面/切后台时补写最后位置
    const flush = () => {
      const time = readTime();
      if (time != null && time > 0) saveProgress(time, currentSegmentIndex(), {force: true});
    };
    window.addEventListener('pagehide', flush);
    window.addEventListener('beforeunload', flush);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flush();
    });

    // 句段变化时刷新状态条（不改任何播放行为）
    document.addEventListener('cet:sentence-change', updateBar);
  }

  function watchForPlayer() {
    if (S.playerWatchTimer) return;
    const startedAt = Date.now();
    S.playerWatchTimer = window.setInterval(() => {
      if (hasPlayer()) {
        window.clearInterval(S.playerWatchTimer);
        S.playerWatchTimer = null;
        bindPlayerListeners();
        updateBar();
        return;
      }
      if (Date.now() - startedAt > CFG.playerWaitMs) {
        window.clearInterval(S.playerWatchTimer);
        S.playerWatchTimer = null;
        log('等待播放器超时');
      }
    }, CFG.playerPollMs);
  }

  // ── 快捷键（返修2 P2-1…P2-4）────────────────────────────────
  // 归属契约（三态，返修2 起生效）：
  //   CET 模式 + 队友模块**关闭** → 底座接管 Space/←/→/R/L，ownsSpaceKey = true
  //   CET 模式 + 队友模块**开启** → 底座一个都不管，   ownsSpaceKey = false
  //   通用模式（ELT_CET_MODE=0）  → 不注册（原版没有快捷键），ownsSpaceKey = false
  // 为什么关闭态要接管：队友模块默认不挂载（cet_mode.cet_listening_agent kept=False），
  // 旧契约「空格一律让给队友」在默认配置下等于工作区**没有任何播放快捷键**。
  // 为什么开启态必须让出：队友模块的 onKeyDown 在 capture 阶段抢 Space 做卡点标记，
  // 两边同时处理会出现「空格既标记又暂停」的双响应。
  const HOTKEY_HINTS = [
    ['Space', '播放 / 暂停'],
    ['← / →', '上一句 / 下一句'],
    ['R', '重听本句'],
    ['L', '本句循环开关'],
  ];
  // 句尾判定容差：timeupdate 粒度约 250ms，用 0.02s 容差避免「刚好卡在句尾」
  // 漏判（任务书要求 currentTime >= 句尾即回跳）。
  const LOOP_EPSILON = 0.02;
  // 回跳后的静默窗：seek 会再触发一次 timeupdate，不设窗会反复 seek（抖动）
  const LOOP_GUARD_MS = 260;

  function cetListeningEnabled() {
    // 队友模块启用即 body.cet-listening；或工具栏已挂载
    return document.body.classList.contains('cet-listening') || Boolean(document.getElementById('cet-toolbar'));
  }

  /** 底座此刻是否接管播放快捷键。**实时判定**，不是挂载时的快照。 */
  function hotkeyActive() {
    return CET_ON && !cetListeningEnabled();
  }

  function sentences() {
    const api = ws();
    return api && Array.isArray(api.sentences) ? api.sentences : [];
  }

  /** 按时间定位当前句（与 workspace.html 的 findUnitByTime 同一口径）。 */
  function sentenceIndexAt(time) {
    const list = sentences();
    if (!list.length) return -1;
    const target = Number(time || 0) + LOOP_EPSILON;
    let found = -1;
    for (let i = 0; i < list.length; i += 1) {
      if (Number(list[i].start || 0) <= target) found = i;
    }
    return found;
  }

  function seekToSeconds(seconds) {
    const target = Number(seconds) || 0;
    const api = ws();
    if (api && typeof api.seek === 'function') {
      api.seek(target);                       // 走页面唯一实现，不另建播放状态
      return;
    }
    const el = audioEl();
    if (el) {
      try { el.currentTime = target; } catch (_) { /* 元数据未就绪，忽略 */ }
    }
  }

  function togglePlay() {
    const api = ws();
    const el = audioEl();
    const paused = el ? Boolean(el.paused) : true;
    if (paused) {
      if (api && typeof api.play === 'function') api.play();
      else if (el && typeof el.play === 'function') el.play().catch(() => {});
    } else if (api && typeof api.pause === 'function') {
      api.pause();
    } else if (el && typeof el.pause === 'function') {
      el.pause();
    }
  }

  function setLoopAnchor(index) {
    const unit = sentences()[index];
    if (!unit) { S.loopAnchor = null; return; }
    S.loopAnchor = {
      index,
      start: Number(unit.start || 0),
      end: Number(unit.end != null ? unit.end : unit.start || 0),
    };
  }

  function toggleLoop() {
    S.loopEnabled = !S.loopEnabled;
    if (!S.loopEnabled) {
      S.loopAnchor = null;
      S.loopGuardUntil = 0;
      stopLoopWatcher();
      syncLoopIndicator();
      return;
    }
    const list = sentences();
    if (!list.length) {
      // 没有逐句时间轴：开关无意义，直接回退，不给假承诺
      S.loopEnabled = false;
      syncLoopIndicator();
      log('没有逐句时间轴，句循环不可用');
      return;
    }
    let idx = sentenceIndexAt(readTime());
    if (idx < 0) idx = 0;
    setLoopAnchor(idx);
    const anchor = S.loopAnchor;
    const now = readTime();
    // 已经越过该句句尾（例如手动拖到后面）：先回到句首，否则「开了却没反应」
    if (anchor && now != null && Number(now) >= anchor.end) seekToSeconds(anchor.start);
    startLoopWatcher();
    syncLoopIndicator();
    log('句循环开启', anchor);
  }

  /** 句循环执行体：跨过当前句句尾就回句首。timeupdate 与 rAF 都调它。 */
  function enforceLoop() {
    if (!S.loopEnabled || !S.loopAnchor || !hotkeyActive()) return;
    if (Date.now() < S.loopGuardUntil) return;      // 刚回跳，等这一轮过去
    const el = audioEl();
    if (!el || el.paused) return;                   // 暂停时不回跳（用户可能正停在句尾看原文）
    const now = Number(el.currentTime || 0);
    const {start, end} = S.loopAnchor;
    if (!Number.isFinite(end) || end <= start) return;
    if (now >= end - LOOP_EPSILON) {
      S.loopGuardUntil = Date.now() + LOOP_GUARD_MS;
      seekToSeconds(start);
      log('句循环回跳', now, '→', start);
    }
  }

  /**
   * 句循环的精确驱动。
   * 为什么不能只靠 timeupdate：浏览器 timeupdate 约 250ms 一次，句尾最多会
   * 多播 250ms 下一句的开头（实测听感明显）。开着循环时用 rAF 逐帧比对，
   * 回跳误差降到一帧（~16ms）。timeupdate 里仍留一份兜底（后台标签页 rAF 会停）。
   */
  function startLoopWatcher() {
    if (S.loopRaf) return;
    const tick = () => {
      if (!S.loopEnabled || !hotkeyActive()) {
        S.loopRaf = null;
        return;
      }
      enforceLoop();
      S.loopRaf = window.requestAnimationFrame(tick);
    };
    S.loopRaf = window.requestAnimationFrame(tick);
  }

  function stopLoopWatcher() {
    if (!S.loopRaf) return;
    window.cancelAnimationFrame(S.loopRaf);
    S.loopRaf = null;
  }


  function stepSentence(delta) {
    const list = sentences();
    if (!list.length) return;
    const idx = sentenceIndexAt(readTime());
    let next;
    if (delta > 0) next = idx < 0 ? 0 : Math.min(idx + 1, list.length - 1);
    else next = idx <= 0 ? 0 : idx - 1;
    seekToSeconds(Number(list[next].start || 0));
    // 句循环跟着用户走：循环的永远是「当前在听的那一句」
    if (S.loopEnabled) setLoopAnchor(next);
  }

  function replaySentence() {
    const list = sentences();
    if (!list.length) return;
    let idx = sentenceIndexAt(readTime());
    if (idx < 0) idx = 0;
    if (S.loopEnabled) setLoopAnchor(idx);
    const start = Number(list[idx].start || 0);
    // 记下「重听落点」，供测试/调试精确断言。自动播放会让 currentTime
    // 立刻往前走，只靠事后采样分不清「回到句首」和「停在中途」。
    S.lastReplay = {index: idx, start};
    seekToSeconds(start);
    const el = audioEl();
    if (el && el.paused) togglePlay();   // 暂停时按 R = 直接重听这一句
  }

  function isTypingTarget(target) {
    if (!target || !target.tagName) return false;
    const tag = String(target.tagName).toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    if (target.isContentEditable) return true;
    return Boolean(target.closest && target.closest('[contenteditable="true"], [contenteditable=""]'));
  }

  /** 按钮/链接上的空格是浏览器原生「点击」，抢了就会双响应。 */
  function isButtonLike(target) {
    if (!target || !target.tagName) return false;
    const tag = String(target.tagName).toLowerCase();
    if (tag === 'button' || tag === 'a' || tag === 'summary') return true;
    return Boolean(target.closest && target.closest('button, a, [role="button"]'));
  }

  function onHotkeyKeyDown(event) {
    if (!hotkeyActive()) return;                 // 让出态/通用模式：一个键都不碰
    if (event.defaultPrevented) return;          // 别人（含队友 capture 监听）已处理
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (isTypingTarget(event.target)) return;

    const isSpace = event.code === 'Space' || event.key === ' ' || event.key === 'Spacebar';
    if (isSpace && isButtonLike(event.target)) return;

    if (isSpace) {
      if (event.repeat) return;                  // 长按不连发翻转
      event.preventDefault();
      S.keyHandled += 1;
      S.lastKey = 'Space';
      togglePlay();
      updateBar();
      return;
    }

    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      S.keyHandled += 1;
      S.lastKey = event.key;
      stepSentence(event.key === 'ArrowRight' ? 1 : -1);
      updateBar();
      return;
    }

    if (event.key === 'r' || event.key === 'R') {
      if (event.repeat) return;
      event.preventDefault();
      S.keyHandled += 1;
      S.lastKey = 'R';
      replaySentence();
      updateBar();
      return;
    }

    if (event.key === 'l' || event.key === 'L') {
      if (event.repeat) return;
      event.preventDefault();
      S.keyHandled += 1;
      S.lastKey = 'L';
      toggleLoop();
      updateBar();
    }
  }

  // ── P2-3：「⌨ 快捷键」提示（只在底座接管态显示）─────────────
  /**
   * 显隐助手：**必须同时**设 hidden 属性与行内 display。
   * 原因（实测踩到的真坑）：cet-workspace.css 的
   *   .cet-study-bar .cet-study-item { display: inline-flex; }
   * 是作者样式，优先级高于 UA 的 `[hidden] { display: none }`，
   * 所以只设 hidden 属性元素照样显示（Playwright is_hidden() 也判为可见）。
   * 行内 display 优先级最高，且置空后回落到 class 的 inline-flex，不会残留样式。
   */
  function setShown(el, show) {
    if (!el) return;
    el.hidden = !show;
    el.style.display = show ? '' : 'none';
  }

  function closeHotkeyPanel() {
    const btn = document.getElementById('cet-hotkey-btn');
    const panel = document.getElementById('cet-hotkey-panel');
    setShown(panel, false);
    if (btn) {
      btn.setAttribute('aria-expanded', 'false');
      btn.dataset.pinned = '0';
    }
  }

  function buildHotkeyHint(bar) {
    if (!bar || bar.querySelector('#cet-hotkey-hint')) return;
    const wrap = document.createElement('span');
    wrap.className = 'cet-study-item cet-hotkey-hint';
    wrap.id = 'cet-hotkey-hint';
    // 初始隐藏：display 先置 none，syncHotkeyHint() 决定是否显示
    wrap.hidden = true;
    wrap.style.cssText = 'position:relative;display:none;align-items:center;';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'cet-hotkey-btn';
    btn.textContent = '⌨ 快捷键';
    btn.title = '查看工作区快捷键';
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-controls', 'cet-hotkey-panel');
    btn.dataset.pinned = '0';
    btn.style.cssText = 'min-height:26px;padding:2px 9px;font-size:12px;border-radius:7px;'
      + 'border:1px solid var(--line,#dfe4dc);background:var(--surface-alt,#f4f6f2);'
      + 'color:var(--brand-dark,#2f5d3a);cursor:pointer;';

    const panel = document.createElement('span');
    panel.id = 'cet-hotkey-panel';
    panel.setAttribute('role', 'tooltip');
    panel.hidden = true;
    panel.style.cssText = 'position:absolute;left:0;top:calc(100% + 6px);z-index:60;min-width:210px;'
      + 'padding:8px 10px;border-radius:9px;border:1px solid var(--line,#dfe4dc);'
      + 'background:var(--surface,#fff);box-shadow:0 8px 22px rgba(0,0,0,.14);'
      + 'font-size:12px;line-height:1.7;color:var(--ink,#22301f);text-align:left;';
    panel.innerHTML = HOTKEY_HINTS.map(([keys, desc]) => (
      '<span style="display:flex;gap:10px;justify-content:space-between;white-space:nowrap">'
      + `<kbd style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;`
      + 'padding:0 5px;border-radius:5px;border:1px solid var(--line,#dfe4dc);background:#f4f6f2">'
      + `${keys}</kbd>`
      + `<span style="color:var(--muted,#6b7a68)">${desc}</span></span>`
    )).join('');

    const setOpen = open => {
      setShown(panel, open);
      btn.setAttribute('aria-expanded', String(open));
      if (!open) btn.dataset.pinned = '0';
    };

    wrap.addEventListener('mouseenter', () => {
      if (wrap.hidden) return;
      setShown(panel, true);
      btn.setAttribute('aria-expanded', 'true');
    });
    wrap.addEventListener('mouseleave', () => {
      if (btn.dataset.pinned === '1') return;    // 点击展开后 hover 移开不收起
      setOpen(false);
    });
    btn.addEventListener('click', event => {
      event.stopPropagation();
      const pinned = btn.dataset.pinned === '1';
      if (pinned) {
        setOpen(false);
      } else {
        setOpen(true);
        btn.dataset.pinned = '1';
      }
    });
    document.addEventListener('click', event => {
      if (!wrap.contains(event.target)) setOpen(false);
    });

    wrap.appendChild(btn);
    wrap.appendChild(panel);
    // 放在「从头开始」按钮之前，和状态项同一行
    const actions = bar.querySelector('.cet-study-actions');
    if (actions) actions.insertAdjacentElement('beforebegin', wrap);
    else bar.appendChild(wrap);
  }

  function syncHotkeyHint() {
    const hint = document.getElementById('cet-hotkey-hint');
    if (!hint) return;
    const show = Boolean(S.ownsSpaceKey);
    setShown(hint, show);
    if (!show) closeHotkeyPanel();
  }

  function syncLoopIndicator() {
    const el = S.barEl ? S.barEl.querySelector('#cet-loop-indicator') : null;
    setShown(el, S.loopEnabled && Boolean(S.ownsSpaceKey));
  }

  /**
   * 归属同步：按当前状态**真实地**注册/注销 keydown，并刷新 ownsSpaceKey。
   * 队友模块可能晚于底座挂载（它的 boot() 要先等两个接口），
   * 所以不能只在 mount() 判一次 —— updateBar()/定时器里也会调到这里。
   */
  function syncHotkeyOwnership() {
    const active = hotkeyActive();
    if (active && !S.keydownBound) {
      document.addEventListener('keydown', onHotkeyKeyDown);
      S.keydownBound = true;
      log('底座接管播放快捷键');
    } else if (!active && S.keydownBound) {
      document.removeEventListener('keydown', onHotkeyKeyDown);
      S.keydownBound = false;
      // 让出时停掉句循环：否则底座还在 seek，和队友模块抢播放位置
      S.loopEnabled = false;
      S.loopAnchor = null;
      S.loopGuardUntil = 0;
      stopLoopWatcher();
      log('底座让出播放快捷键');
    }
    if (S.ownsSpaceKey !== active) {
      S.ownsSpaceKey = active;
      document.body.dataset.cetHotkeys = active ? 'on' : 'off';
      syncHotkeyHint();
      syncLoopIndicator();
    }
    return active;
  }

  // ── 挂载 ────────────────────────────────────────────────────
  function mount() {
    if (S.mounted) return;          // 幂等：只挂一次
    S.mounted = true;
    document.body.dataset.cetMode = CET_ON ? 'on' : 'off';
    document.body.dataset.cetLessonId = String(LESSON_ID || '');
    wrapPlayerControls();
    buildBar();
    // 状态条建好后立刻定归属：关闭态接管、开启态让出、通用模式不注册
    syncHotkeyOwnership();
    updateBar();
    bindPageListeners();
    if (hasPlayer()) bindPlayerListeners();
    else watchForPlayer();
    // 播放器被重建（切换精听/阅读）时补绑
    window.setInterval(() => {
      wrapPlayerControls();
      if (hasPlayer() && !audioEl()?.dataset.cetProgressBound) bindPlayerListeners();
      // 队友模块可能在底座之后才挂载/卸载：周期重判归属，保证不抢也不漏
      syncHotkeyOwnership();
    }, 1500);
  }

  function boot() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', mount, {once: true});
    } else {
      mount();
    }
  }

  boot();

  // 暴露给测试与其它模块（只读为主）
  window.__cetPlayer = {
    version: '20261106-p2',
    lessonId: LESSON_ID,
    cetOn: CET_ON,
    features: FEATURES,
    copy: COPY,
    mount,
    isMounted: () => S.mounted,
    updateBar,
    restorePosition,
    saveProgress: (seconds, segmentIndex) => saveProgress(seconds, segmentIndex, {force: true}),
    fetchProgress,
    currentSegmentIndex,
    hasPlayer,
    audioEl,
    cetListeningEnabled,
    // ── 快捷键归属（返修2 P2）──
    // ownsSpaceKey **不是常量**：它反映「底座此刻是否真的接管了 Space」。
    //   CET 模式 + 队友关闭 → true（底座注册了 keydown）
    //   CET 模式 + 队友开启 → false（底座已注销 keydown，让给队友）
    //   通用模式            → false（原版没有快捷键，行为不变）
    // 取值来自 S.ownsSpaceKey，由 syncHotkeyOwnership() 在 mount/updateBar/定时器里
    // 与实际注册状态一起同步，避免「声明 true 但没绑」或「绑了却声明 false」。
    get ownsSpaceKey() { return Boolean(S.ownsSpaceKey); },
    hotkeyActive,
    syncHotkeyOwnership,
    togglePlay,
    stepSentence,
    replaySentence,
    toggleLoop,
    sentenceIndexAt,
    _state: S,
  };
})();
