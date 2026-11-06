/* 四六级听力底座 · 收藏适配层（cet-favorites.js）
 *
 * 职责（任务书 §4 P3）：
 *   - 收藏词句保留材料身份、原句和已有音频定位信息
 *   - 移除收藏不删除原材料
 *   - 收藏返回原文/原声确实跳转；缺少时间轴或音频不可用有明确反馈
 *
 * 复用现有接口，不新增后端：
 *   GET    /api/v2/lessons/{id}/words               词汇收藏
 *   DELETE /api/v2/lessons/{id}/word/{word}
 *   GET    /api/v2/lessons/{id}/phase-b             句子收藏
 *   DELETE /api/v2/lessons/{id}/phase-b/{segment}
 *   GET    /api/cet/materials/by-lesson/{id}        材料身份（非 CET 材料 404，静默降级）
 *
 * 只读 window.__ws 的播放控制，不自己碰媒体元素，避免第二套播放状态。
 */
(function () {
  'use strict';

  const BOOT = window.__CET_BOOT__ || {enabled: false, features: {}, copy: {}, lessonId: null};
  const CET_ON = Boolean(BOOT.enabled);
  const FEATURES = BOOT.features || {};
  const LESSON_ID = Number(BOOT.lessonId || window.__ws?.lessonId || 0) || null;

  const CFG = {
    wordsUrl: LESSON_ID ? `/api/v2/lessons/${LESSON_ID}/words` : '',
    sentencesUrl: LESSON_ID ? `/api/v2/lessons/${LESSON_ID}/phase-b` : '',
    materialUrl: LESSON_ID ? `/api/cet/materials/by-lesson/${LESSON_ID}` : '',
  };

  const S = {
    mounted: false,
    rootEl: null,
    statusEl: null,
    words: [],
    meanings: {},
    sentences: [],
    material: null,
    materialUnavailable: false,
    loading: false,
    lastError: '',
    jumpToken: 0,
    // P1-5：workspace 是否已把词句数据送来过（送过就不再自己发请求）
    gotInitialData: false,
  };

  const $ = sel => document.querySelector(sel);

  function log(...args) {
    if (window.__CET_DEBUG__) console.info('[cet-favorites]', ...args);
  }

  function fmt(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function ws() { return window.__ws || null; }

  // ── 材料身份 ────────────────────────────────────────────────
  async function loadMaterial() {
    if (!CFG.materialUrl) return null;
    try {
      const resp = await fetch(CFG.materialUrl, {cache: 'no-store'});
      if (resp.status === 404) {
        // 非四六级材料：底座仍然可用，只是没有材料业务身份
        S.materialUnavailable = true;
        return null;
      }
      if (!resp.ok) return null;
      const data = await resp.json().catch(() => ({}));
      S.material = {
        materialId: data.materialId || '',
        materialVersion: data.materialVersion || '',
        level: data.level || 'unspecified',
        title: data.title || '',
        audioUrl: data.audioUrl || '',
      };
      return S.material;
    } catch (_) {
      return null;
    }
  }

  function levelLabel(level) {
    return ({cet4: '四级', cet6: '六级', unspecified: '待分类'})[level] || '待分类';
  }

  function materialLine() {
    const api = ws();
    const title = S.material?.title || (api ? api.title : '') || '当前材料';
    const level = S.material ? levelLabel(S.material.level) : '待分类';
    const id = S.material?.materialId || (LESSON_ID ? `v2-${LESSON_ID}` : '—');
    return {title, level, id};
  }

  // ── 数据加载 ────────────────────────────────────────────────
  async function loadAll() {
    if (S.loading) return;
    S.loading = true;
    try {
      await loadMaterial();
      const [wordsData, sentencesData] = await Promise.all([
        CFG.wordsUrl ? fetch(CFG.wordsUrl, {cache: 'no-store'}).then(r => (r.ok ? r.json() : null)).catch(() => null) : null,
        CFG.sentencesUrl ? fetch(CFG.sentencesUrl, {cache: 'no-store'}).then(r => (r.ok ? r.json() : null)).catch(() => null) : null,
      ]);
      S.words = (wordsData?.words || []).slice();
      S.meanings = wordsData?.meanings || {};
      S.sentences = (sentencesData?.sentences || []).slice();
      S.lastError = '';
    } catch (error) {
      S.lastError = error?.message || '收藏加载失败';
    } finally {
      S.loading = false;
      render();
      notifyCount();
    }
  }

  // ── 跳转：回到原声/原句 ─────────────────────────────────────
  function setStatus(message, isError = false) {
    if (!S.statusEl) return;
    S.statusEl.textContent = message || '';
    S.statusEl.classList.toggle('cet-fav-status--error', Boolean(isError));
  }

  function flashSentence(index) {
    const api = ws();
    const sentences = api ? api.sentences : [];
    const unit = sentences[index];
    if (!unit) return;
    // 原文卡片高亮一下，让「确实跳回去了」可见
    const card = document.querySelector('.subtitle-card');
    if (!card) return;
    card.classList.remove('cet-fav-target-flash');
    void card.offsetWidth;
    card.classList.add('cet-fav-target-flash');
    window.setTimeout(() => card.classList.remove('cet-fav-target-flash'), 1700);
  }

  /**
   * 跳到某个句索引并播放。
   * 明确区分三种失败：没有播放器 / 没有时间轴 / 时间轴越界。
   */
  function jumpToSentence(index, {autoplay = true} = {}) {
    const api = ws();
    const token = ++S.jumpToken;
    if (!api) {
      setStatus('工作区未就绪，暂时无法跳转。', true);
      return false;
    }
    if (!api.mediaUrl) {
      setStatus('这份材料没有可用音频，只能查看原文，无法回到原声。', true);
      return false;
    }
    const sentences = api.sentences || [];
    if (!sentences.length) {
      setStatus('这份材料暂无逐句时间轴，无法定位到原句。可整段播放后手动拖动。', true);
      return false;
    }
    const idx = Math.max(0, Math.min(sentences.length - 1, Number(index)));
    const unit = sentences[idx];
    const start = Number(unit?.start);
    if (!Number.isFinite(start) || start < 0) {
      setStatus('这句缺少可用的时间轴，无法定位原声。', true);
      return false;
    }
    const duration = Number(api.duration || 0);
    if (duration > 0 && start > duration) {
      setStatus('这句的时间点超出音频长度，可能材料已替换。', true);
      return false;
    }
    api.seek(start);
    if (autoplay && typeof api.play === 'function') api.play();
    if (token === S.jumpToken) {
      flashSentence(idx);
      setStatus(`已跳到 ${fmt(start)}（第 ${idx + 1} 句）`);
    }
    return true;
  }

  // 词 → 所在句：优先当前句，其次全文第一个含该词的句子（不拿无关句当上下文）
  function sentenceForWord(word) {
    const api = ws();
    const needle = String(word || '').toLowerCase();
    if (!api || !needle) return -1;
    const sentences = api.sentences || [];
    const tokensOf = text => (String(text || '').toLowerCase().match(/[a-z]+(?:'[a-z]+)?/g) || []);
    const active = api.activeSentenceIndex >= 0 ? api.activeSentenceIndex : -1;
    if (active >= 0 && sentences[active] && tokensOf(sentences[active].text).includes(needle)) return active;
    return sentences.findIndex(unit => tokensOf(unit.text).includes(needle));
  }

  // ── 移除收藏（不删除原材料）─────────────────────────────────
  async function removeWord(word, button) {
    if (!word || !LESSON_ID) return;
    if (button) button.disabled = true;
    try {
      const resp = await fetch(`/api/v2/lessons/${LESSON_ID}/word/${encodeURIComponent(word)}`, {method: 'DELETE'});
      if (!resp.ok) throw new Error(`移除失败（HTTP ${resp.status}）`);
      S.words = S.words.filter(item => item !== word);
      delete S.meanings[word];
      render();
      notifyCount();
      setStatus(`已从收藏移除「${word}」。材料本身不受影响。`);
      // 让原页面的词集状态同步刷新（保留单一数据源）
      if (typeof window.loadSavedWords === 'function') window.loadSavedWords().catch(() => {});
    } catch (error) {
      if (button) button.disabled = false;
      setStatus(error?.message || '移除失败', true);
    }
  }

  async function removeSentence(sentence, button) {
    if (!sentence || !LESSON_ID) return;
    if (button) button.disabled = true;
    try {
      const resp = await fetch(
        `/api/v2/lessons/${LESSON_ID}/phase-b/${encodeURIComponent(sentence.segment_index)}`,
        {method: 'DELETE'},
      );
      if (!resp.ok) throw new Error(`移除失败（HTTP ${resp.status}）`);
      S.sentences = S.sentences.filter(item => String(item.segment_index) !== String(sentence.segment_index));
      render();
      notifyCount();
      setStatus('已从收藏移除该句。材料本身不受影响。');
      if (typeof window.loadSavedSentences === 'function') window.loadSavedSentences().catch(() => {});
    } catch (error) {
      if (button) button.disabled = false;
      setStatus(error?.message || '移除失败', true);
    }
  }

  // ── 渲染 ────────────────────────────────────────────────────
  function notifyCount() {
    const total = S.words.length + S.sentences.length;
    const el = document.getElementById('cet-saved-count');
    if (el) {
      el.textContent = String(total);
      el.dataset.filled = '1';
    }
    const tab = document.querySelector('.sidebar-workspace-tab[data-sidebar-target="collection"]');
    if (tab) {
      tab.setAttribute('aria-label', `收藏，共 ${total} 项`);
    }
  }

  function render() {
    if (!S.rootEl) return;
    const {title, level, id} = materialLine();
    const hasAudio = Boolean(ws()?.mediaUrl);
    const hasTimeline = (ws()?.sentences || []).length > 0;

    if (!S.words.length && !S.sentences.length) {
      S.rootEl.innerHTML = `
        <div class="cet-fav-empty">
          ${escapeHtml(title)}（${escapeHtml(level)}）还没有收藏。<br>
          播放时点句中的词查义后收藏单词，或用「当前句」旁的星标收藏整句。
        </div>`;
      return;
    }

    const wordHtml = S.words.map(word => {
      const meaning = S.meanings[word] || '';
      const sentIdx = sentenceForWord(word);
      const canJump = hasAudio && hasTimeline && sentIdx >= 0;
      const meta = [];
      meta.push(`<span>${escapeHtml(level)}</span>`);
      meta.push(`<span>材料 ${escapeHtml(id)}</span>`);
      if (sentIdx >= 0) {
        const unit = (ws()?.sentences || [])[sentIdx];
        meta.push(`<span>第 ${sentIdx + 1} 句 · ${fmt(unit?.start)}</span>`);
      } else {
        meta.push('<span class="cet-fav-warn">原文中未找到该词</span>');
      }
      return `<div class="cet-fav-item" data-cet-fav-word="${escapeHtml(word)}">
        <div class="cet-fav-body">
          <span class="cet-fav-text"><strong>${escapeHtml(word)}</strong>${meaning ? ` · ${escapeHtml(meaning)}` : ''}</span>
          <span class="cet-fav-meta">${meta.join('')}</span>
        </div>
        <div class="cet-fav-actions">
          <button type="button" class="cet-fav-jump" data-cet-jump-word="${escapeHtml(word)}" ${canJump ? '' : 'disabled'}
                  title="${canJump ? '跳到该词所在句的原声' : '缺少时间轴或音频，无法定位'}">回到原句</button>
          <button type="button" class="cet-fav-remove" data-cet-remove-word="${escapeHtml(word)}">移除收藏</button>
        </div>
      </div>`;
    }).join('');

    const sentenceHtml = S.sentences.map(item => {
      const start = Number(item.start_seconds);
      const end = Number(item.end_seconds);
      const hasRange = Number.isFinite(start) && Number.isFinite(end) && end > start;
      const canJump = hasAudio && hasRange;
      const meta = [`<span>${escapeHtml(level)}</span>`, `<span>材料 ${escapeHtml(id)}</span>`];
      if (hasRange) meta.push(`<span>${fmt(start)} – ${fmt(end)}</span>`);
      else meta.push('<span class="cet-fav-warn">缺少时间轴</span>');
      meta.push(`<span>第 ${Number(item.segment_index) + 1} 句</span>`);
      return `<div class="cet-fav-item cet-fav-item--sentence" data-cet-fav-sentence="${escapeHtml(item.segment_index)}">
        <div class="cet-fav-body">
          <span class="cet-fav-text">${escapeHtml(item.text || '')}</span>
          <span class="cet-fav-meta">${meta.join('')}</span>
        </div>
        <div class="cet-fav-actions">
          <button type="button" class="cet-fav-jump" data-cet-jump-sentence="${escapeHtml(item.segment_index)}" ${canJump ? '' : 'disabled'}
                  title="${canJump ? '跳到这句的原声' : '缺少时间轴或音频，无法定位'}">回到原声</button>
          <button type="button" class="cet-fav-remove" data-cet-remove-sentence="${escapeHtml(item.segment_index)}">移除收藏</button>
        </div>
      </div>`;
    }).join('');

    S.rootEl.innerHTML = `
      <div class="cet-fav-group-title">词汇收藏 · ${S.words.length}</div>
      ${wordHtml || '<div class="cet-fav-empty">还没有收藏的单词。</div>'}
      <div class="cet-fav-group-title">句子收藏 · ${S.sentences.length}</div>
      ${sentenceHtml || '<div class="cet-fav-empty">还没有收藏的句子。</div>'}`;
  }

  // 事件委托：列表会重渲染，绑在容器上只挂一次
  function bindDelegation() {
    if (!S.rootEl || S.rootEl.dataset.cetBound === '1') return;
    S.rootEl.dataset.cetBound = '1';
    S.rootEl.addEventListener('click', event => {
      const jumpWord = event.target.closest('[data-cet-jump-word]');
      if (jumpWord && !jumpWord.disabled) {
        const word = jumpWord.dataset.cetJumpWord;
        const idx = sentenceForWord(word);
        if (idx < 0) {
          setStatus(`「${word}」不在当前材料的原文里，无法定位。`, true);
          return;
        }
        jumpToSentence(idx);
        return;
      }
      const jumpSentence = event.target.closest('[data-cet-jump-sentence]');
      if (jumpSentence && !jumpSentence.disabled) {
        const segmentIndex = Number(jumpSentence.dataset.cetJumpSentence);
        // 收藏里记的是 segment_index；映射到当前句索引
        const api = ws();
        const sentences = api ? api.sentences : [];
        const target = sentences.findIndex(unit => (unit.segment_ids || []).includes(segmentIndex));
        if (target < 0) {
          setStatus('这句已不在当前材料的时间轴里（材料可能已替换）。', true);
          return;
        }
        jumpToSentence(target);
        return;
      }
      const removeWordBtn = event.target.closest('[data-cet-remove-word]');
      if (removeWordBtn) {
        removeWord(removeWordBtn.dataset.cetRemoveWord, removeWordBtn);
        return;
      }
      const removeSentenceBtn = event.target.closest('[data-cet-remove-sentence]');
      if (removeSentenceBtn) {
        const segmentIndex = removeSentenceBtn.dataset.cetRemoveSentence;
        const item = S.sentences.find(entry => String(entry.segment_index) === String(segmentIndex));
        if (item) removeSentence(item, removeSentenceBtn);
      }
    });
  }

  // ── 挂载 ────────────────────────────────────────────────────
  function buildRoot() {
    if (S.rootEl && S.rootEl.isConnected) return S.rootEl;
    const panel = document.getElementById('reading-phase-b');
    if (!panel) return null;
    const container = document.getElementById('study-collection') || panel;

    const root = document.createElement('div');
    root.className = 'cet-fav-list';
    root.id = 'cet-favorites';
    root.setAttribute('aria-label', '我的收藏');
    container.insertBefore(root, container.firstChild);

    const status = document.createElement('div');
    status.className = 'cet-fav-status';
    status.id = 'cet-fav-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    root.insertAdjacentElement('afterend', status);

    S.rootEl = root;
    S.statusEl = status;

    // CET 模式下由本模块接管收藏列表渲染（原列表无跳转能力）；
    // 原 review-body（计数/导出/标签）保留不动。
    if (CET_ON) {
      const legacy = document.getElementById('reading-saved-items');
      if (legacy) legacy.hidden = true;
    }
    return root;
  }

  /**
   * P1-5（返修2 包1）：直接吃 workspace 已经加载好的数据，不再自己发请求。
   *
   * 改前：mount() 无条件 loadAll() → 打 `/words` + `/phase-b`，
   * 而 workspace.html 的 bootstrap `refreshLesson()` 也刚打过同样两个接口，
   * 于是首次打开工作区这两个接口各被请求 2 次（playwright 调用栈实测确认）。
   * 改后：workspace 在数据就绪后派发 `cet:favorites-changed` 并带上 detail，
   * 本模块直接采用，零额外请求；detail 缺失时（例如其它调用方触发的写操作刷新）
   * 才回退到 loadAll()，保证「移除收藏 / 收藏动作后列表仍会刷新」不变。
   */
  function applySnapshot(detail) {
    if (!detail) return false;
    const hasWords = Array.isArray(detail.words);
    const hasSentences = Array.isArray(detail.sentences);
    if (!hasWords && !hasSentences) return false;
    S.words = (detail.words || []).slice();
    S.meanings = detail.meanings || {};
    S.sentences = (detail.sentences || []).slice();
    S.lastError = '';
    S.gotInitialData = true;
    render();
    notifyCount();
    return true;
  }

  function onFavoritesChanged(event) {
    // 有 workspace 送来的数据就不发请求；没有才自己拉
    if (applySnapshot(event && event.detail)) return;
    loadAll();
  }

  function mount() {
    if (S.mounted) return;   // 幂等
    S.mounted = true;
    if (!buildRoot()) {
      log('未找到收藏面板，跳过');
      return;
    }
    bindDelegation();
    // 材料业务身份（/api/cet/materials/by-lesson/{id}）workspace 不加载，仍由本模块取；
    // 词句数据等 workspace 的 `cet:favorites-changed` 送来（见 applySnapshot）。
    loadMaterial().then(() => { if (S.gotInitialData) render(); });
    // 收藏里的「回到原句」依赖 window.__ws.sentences，而它在 /subtitles 回来后才非空。
    // 首次渲染可能早于字幕就绪，所以这里等句子索引可用后重渲染一次（幂等）。
    waitForSentencesThenRerender();
    document.addEventListener('cet:favorites-changed', onFavoritesChanged);
    // 兜底：workspace 的派发因任何原因没到（例如脚本执行顺序异常），
    // 3 秒后仍没有数据就自己拉一次 —— 宁多一次请求，也不留一个空面板。
    window.setTimeout(() => {
      if (!S.gotInitialData && !S.loading) loadAll();
    }, 3000);
  }

  function waitForSentencesThenRerender() {
    let tries = 0;
    const timer = window.setInterval(() => {
      tries += 1;
      const hasSentences = (ws()?.sentences || []).length > 0;
      if (hasSentences) {
        window.clearInterval(timer);
        render();
        notifyCount();
        return;
      }
      // 最多等 20 秒；没有原文的材料本来就没有句子索引，不必再等
      if (tries > 100) window.clearInterval(timer);
    }, 200);
  }

  function boot() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', mount, {once: true});
    } else {
      mount();
    }
  }

  boot();

  window.__cetFavorites = {
    version: '20261105-p1',
    lessonId: LESSON_ID,
    cetOn: CET_ON,
    mount,
    isMounted: () => S.mounted,
    reload: loadAll,
    jumpToSentence,
    sentenceForWord,
    removeWord,
    removeSentence,
    getState: () => ({
      words: S.words.slice(),
      sentences: S.sentences.slice(),
      material: S.material,
      materialUnavailable: S.materialUnavailable,
      lastError: S.lastError,
    }),
  };
})();
