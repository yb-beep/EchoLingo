/* cet-mode.js — 四六级专用底座 · 前端模式运行时
 *
 * 唯一配置来源：后端 `webapp/cet_mode.py` → `GET /api/cet/mode`。
 * 页面渲染时后端已把同一份配置注入 `window.__CET_BOOT__`（Jinja），
 * 本文件**只读**这份配置，不另立开关：
 *
 *   CET.feature('link_import')  → false 表示 CET 模式已收束
 *   CET.copy('home_heading')    → 全站文案
 *   CET.nav()                   → 导航项
 *
 * 为什么还要再 fetch 一次：
 *   1. 契约要求启动时读 `/api/cet/mode`（服务端为准，注入值是首屏快照）；
 *   2. 若两者不一致（例如 ELT_CET_MODE 在进程运行中被改），以服务端为准，
 *      并且只做「继续收束」，不尝试把已收起的模块再放出来（避免闪烁与半状态）。
 *   接口失败时静默沿用注入值（CET 模式默认值），**不白屏**。
 *
 * 责任边界：
 *   - 本文件负责：hash 路由、导航高亮、被收束静态节点兜底移除、路由兜底提示。
 *   - 各模块的初始化开关由 index.html 里 `CET.feature(...)` 直接判断（同一份配置）。
 */
(function () {
  'use strict';

  // ── CET 模式默认值：与 backend/webapp/cet_mode.py 的注册表一致 ──
  // 仅在页面没有注入 __CET_BOOT__（例如直接打开静态副本）时兜底。
  var DEFAULT_FEATURES = {
    listening_library: true, local_import: true, player: true, transcript: true,
    dictionary: true, word_favorites: true, sentence_favorites: true,
    progress_resume: true, settings_core: true, ai_word_sentence_explain: true,
    link_import: false, reading_course: false, text_to_speech_course: false,
    video_learning: false, memory_story: false, scenario_sentence: false,
    ielts_reuse: false, free_retell: false, ai_chat_general: false,
    outline_generation: false, credits_account: false,
    cet_listening_agent: true
  };
  var DEFAULT_NAV = [
    { key: 'lessons', label: '听力材料', hash: '', icon: '🎧' },
    { key: 'favorites', label: '我的收藏', hash: 'favorites', icon: '⭐' },
    { key: 'resources', label: '设置', hash: 'resources', icon: '⚙️' }
  ];
  var DEFAULT_COPY = {
    site_title: '听透 · 四六级听力',
    home_heading: '四六级听力材料',
    home_subtitle: '找到卡住你的那一句。选择四级或六级，逐句精听。',
    library_heading: '听力材料',
    workspace_title: '听力工作区',
    transcript_label: '原文',
    favorites_heading: '我的收藏',
    vocab_heading: '词汇收藏',
    sentences_heading: '句子收藏',
    settings_heading: '设置',
    empty_library: '还没有听力材料。导入一份本地音频开始学习。',
    unspecified_level: '待分类'
  };

  function _boot() {
    var injected = window.__CET_BOOT__;
    if (!injected || typeof injected !== 'object' || !injected.features) {
      // 后端用 <script type="application/json" id="cet-boot"> 注入同一份配置：
      // 用 JSON 块而不是可执行内联脚本，模板里唯一的可执行内联脚本仍是主脚本。
      var node = document.getElementById('cet-boot');
      if (node) {
        try {
          var parsed = JSON.parse(node.textContent || '{}');
          if (parsed && typeof parsed === 'object' && parsed.features) injected = parsed;
        } catch (_e) { injected = null; }
      }
    }
    if (injected && typeof injected === 'object' && injected.features) return injected;
    // 没有注入：按 CET 模式默认值工作（任务书要求 CET 为默认产品模式）
    var features = {};
    Object.keys(DEFAULT_FEATURES).forEach(function (key) {
      features[key] = {
        enabled: DEFAULT_FEATURES[key], kept: DEFAULT_FEATURES[key],
        label: '', note: ''
      };
    });
    return {
      enabled: true, features: features, nav: DEFAULT_NAV, copy: DEFAULT_COPY,
      wordlists: ['builtin_cet4', 'builtin_cet6'],
      levels: ['cet4', 'cet6', 'unspecified'], retired_keys: []
    };
  }

  var config = _boot();

  function feature(key) {
    var item = config.features && config.features[key];
    if (!item) {
      // 未知 key 不静默当作「启用」：拼错的开关名会让收束悄悄失效。
      // 但前端不能因此抛异常打断页面，退化为「未启用」并显式警告。
      if (window.console && console.warn) console.warn('[cet-mode] unknown feature:', key);
      return false;
    }
    return item.enabled !== false;
  }

  function copy(key, fallback) {
    var value = config.copy && config.copy[key];
    return (value === undefined || value === null || value === '') ? (fallback || '') : value;
  }

  function nav() {
    return Array.isArray(config.nav) ? config.nav : [];
  }

  // CET 模式的主选项词表（任务书 §3.2：其他词表不作主选项，底层资源保留）
  function wordlists() {
    return Array.isArray(config.wordlists) ? config.wordlists : [];
  }

  function levels() {
    return Array.isArray(config.levels) ? config.levels : [];
  }

  function enabled() {
    return config.enabled !== false;
  }

  // ── hash 路由 ────────────────────────────────────────────────
  // 对外 hash：''（听力材料）、#favorites（我的收藏）、#vocab / #sentences
  // （旧链接，落到对应收藏子页）、#resources / #settings（设置）。
  var VIEW_BY_HASH = {
    '': 'lessons',
    'lessons': 'lessons',
    'favorites': 'favorites',
    'library': 'library',
    'review': 'review',
    'vocab': 'vocab',
    'sentences': 'sentences',
    'resources': 'resources',
    'settings': 'resources'
  };
  // 已退出功能的历史 hash：落到首页并给出明确说明，不留空白页/死链接。
  var RETIRED_HASH_HINT = {
    story: '记忆故事',
    stories: '记忆故事',
    planning: '个人档案',
    account: '账号与积分',
    login: '登录',
    register: '注册',
    outline: '内容大纲',
    reading: '阅读课程',
    intensive: '精读课程',
    retell: '复述评分',
    chat: 'AI 聊天'
  };

  var _noticeTimer = null;

  function showNotice(message) {
    var el = document.getElementById('cet-route-notice');
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
    if (_noticeTimer) clearTimeout(_noticeTimer);
    _noticeTimer = setTimeout(function () { el.hidden = true; }, 9000);
  }

  function hideNotice() {
    var el = document.getElementById('cet-route-notice');
    if (el) el.hidden = true;
  }

  function _favoritesPane() {
    try {
      var saved = localStorage.getItem('cet-favorites-pane');
      if (saved === 'sentences' || saved === 'vocab') return saved;
    } catch (_e) { /* 隐私模式下 localStorage 可能抛错 */ }
    return 'vocab';
  }

  function _hashName() {
    return String(location.hash || '').replace(/^#/, '').replace(/^\/+/, '').trim().toLowerCase();
  }

  function _applyRoute(hashName) {
    var view = VIEW_BY_HASH[hashName];
    if (view === 'library' || view === 'review') {
      if (window.matchMedia('(max-width: 700px)').matches && typeof window.tingtouMobileView === 'function') {
        window.tingtouMobileView(view, {keepHistory: true});
      } else if (typeof window.switchTab === 'function') {
        window.switchTab(view === 'review' ? 'favorites' : 'lessons', {keepHash: true});
      }
      return;
    }
    if (view === undefined) {
      // 未知 / 已收束的 hash：回首页 + 说明，绝不停在空白页
      var label = RETIRED_HASH_HINT[hashName];
      if (typeof window.switchTab === 'function') {
        window.switchTab('lessons', { keepHash: true, nav: 'lessons' });
      }
      showNotice(label
        ? '「' + label + '」在四六级听力模式中已收束，已返回听力材料。'
        : '没有这个页面，已返回听力材料。');
      return;
    }
    hideNotice();
    if (view === 'favorites') {
      var pane = _favoritesPane();
      if (typeof window.switchTab === 'function') {
        window.switchTab(pane, { keepHash: true, nav: 'favorites' });
      }
      return;
    }
    if (typeof window.switchTab === 'function') {
      window.switchTab(view, { keepHash: true, nav: view });
    }
  }

  function route() {
    if (!enabled()) return false; // 通用模式沿用原页面自己的 hash 处理
    _applyRoute(_hashName());
    return true;
  }

  function navigate(hash) {
    var target = String(hash || '').replace(/^#/, '');
    var next = target ? '#' + target : '';
    if (location.hash === next) {
      _applyRoute(target.toLowerCase());
      return;
    }
    location.hash = next; // 触发 hashchange → route()
  }

  // ── 兜底移除：Jinja 已按配置决定是否输出，这里只处理注入值与服务端不一致的情况 ──
  function applyRetired() {
    var removed = [];
    document.querySelectorAll('[data-cet-feature]').forEach(function (el) {
      var key = el.getAttribute('data-cet-feature');
      if (!key) return;
      if (feature(key)) return;
      removed.push(key);
      el.remove(); // 真移除，不是 CSS 遮挡：不会被 Tab 聚焦
    });
    return removed;
  }

  // ── 启动 ─────────────────────────────────────────────────────
  function _fetchConfig() {
    if (typeof fetch !== 'function') return Promise.resolve(null);
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, 4000);
    return fetch('/api/cet/mode', {
      cache: 'no-store',
      signal: controller ? controller.signal : undefined
    }).then(function (resp) {
      clearTimeout(timer);
      if (!resp.ok) return null;
      return resp.json();
    }).then(function (data) {
      return (data && typeof data === 'object' && data.features) ? data : null;
    }).catch(function () {
      clearTimeout(timer);
      return null; // 接口失败：沿用注入值，不白屏
    });
  }

  function _reconcile(remote) {
    if (!remote) return;
    var changed = [];
    Object.keys(remote.features || {}).forEach(function (key) {
      var before = feature(key);
      var after = remote.features[key].enabled !== false;
      if (before !== after) changed.push(key);
    });
    config = remote;
    if (changed.length) {
      // 只做「继续收束」，不复活已移除的模块
      applyRetired();
      if (window.console && console.info) {
        console.info('[cet-mode] 服务端配置与首屏注入不一致，已按服务端收束:', changed.join(', '));
      }
    }
    document.documentElement.setAttribute('data-cet-mode', enabled() ? 'on' : 'off');
  }

  function start() {
    if (enabled()) {
      applyRetired();
      route();
      window.addEventListener('hashchange', function () { route(); });
    }
    document.documentElement.setAttribute('data-cet-mode', enabled() ? 'on' : 'off');
    _fetchConfig().then(function (remote) {
      if (!remote) return;
      _reconcile(remote);
      if (enabled()) route(); // 配置变化后重放当前 hash，避免停在已收束视图
    });
  }

  window.CET = {
    get config() { return config; },
    enabled: enabled,
    feature: feature,
    copy: copy,
    nav: nav,
    wordlists: wordlists,
    levels: levels,
    route: route,
    navigate: navigate,
    showNotice: showNotice,
    applyRetired: applyRetired,
    ready: false
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      window.CET.ready = true;
      start();
    });
  } else {
    window.CET.ready = true;
    start();
  }
})();
