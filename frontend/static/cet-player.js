/* 四六级听力底座 · 播放适配层（cet-player.js）
 *
 * 职责（任务书 §4 P3 + §5 P5）：
 *   1. 学习位置恢复：POST/GET /api/v2/lessons/{id}/progress，刷新/重开都回到原位置
 *   2. 材料身份校验：进度与收藏都绑定 lesson id，切换材料不串数据
 *   3. 学习状态条：显示真实句数/进度，不编造掌握度或预测分数
 *   4. 快捷键边界：**空格归队友卡点模块**，本文件不抢 Space
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
    saveTimer: null,
    playerWatchTimer: null,
    listenersBound: false,
    lessonVerified: false,
    saveInFlight: false,
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
    return bar;
  }

  function updateBar() {
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
      // 用户已经开始播放/拖动时不覆盖他的位置
      const alreadyMoved = el ? Number(el.currentTime || 0) > 1 : false;
      if (alreadyMoved) {
        log('用户已开始播放，跳过自动恢复');
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

  // ── 事件绑定（幂等）────────────────────────────────────────
  function bindPlayerListeners() {
    const el = audioEl();
    if (!el || el.dataset.cetProgressBound === '1') return;
    el.dataset.cetProgressBound = '1';

    el.addEventListener('timeupdate', () => {
      updateBar();
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
      updateBar();
    });
    el.addEventListener('loadedmetadata', () => {
      updateBar();
      restorePosition();
    });
    el.addEventListener('seeked', () => {
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

  // ── 快捷键边界：空格归队友模块 ──────────────────────────────
  // 本模块**不注册任何全局 keydown**。这里只提供一条查询接口，供队友模块
  // 与测试确认「底座没有抢占空格」。若将来需要底座快捷键，只允许用
  // 不带 Space 的组合键，并且必须先检查 cetListeningEnabled()。
  function cetListeningEnabled() {
    // 队友模块启用即 body.cet-listening；或工具栏已挂载
    return document.body.classList.contains('cet-listening') || Boolean(document.getElementById('cet-toolbar'));
  }

  // ── 挂载 ────────────────────────────────────────────────────
  function mount() {
    if (S.mounted) return;          // 幂等：只挂一次
    S.mounted = true;
    document.body.dataset.cetMode = CET_ON ? 'on' : 'off';
    document.body.dataset.cetLessonId = String(LESSON_ID || '');
    buildBar();
    updateBar();
    bindPageListeners();
    if (hasPlayer()) bindPlayerListeners();
    else watchForPlayer();
    // 播放器被重建（切换精听/阅读）时补绑
    window.setInterval(() => {
      if (hasPlayer() && !audioEl()?.dataset.cetProgressBound) bindPlayerListeners();
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
    version: '20261105-p1',
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
    // 明确声明：本模块不注册空格快捷键
    ownsSpaceKey: false,
    _state: S,
  };
})();
