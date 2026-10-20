/*
 * LittleBot player page.
 *
 * Plays ONLY the parent-approved item the robot asks for, using the official YouTube
 * IFrame Player API (https://developers.google.com/youtube/iframe_api_reference).
 *
 *  - polls GET  player/api/state  (~2 Hz) for the desired state {seq, action, load_id, item, volume}
 *  - reports    POST player/api/event  (ready, playing, paused, ended, error <code>, heartbeat, ...)
 *  - when a video ENDS the player is destroyed immediately and a calm "All done!" screen is shown,
 *    so YouTube's end screen / related videos never appear
 *  - checks the video actually playing is the approved one (or part of the approved playlist)
 *  - reports when the page is hidden (e.g. a YouTube link opened a new tab/window over it) or
 *    navigated away; the robot then stops and closes the whole kiosk browser
 *  - never draws anything over a live player and never blocks player functionality
 *    (YouTube Required Minimum Functionality / Developer Policies III.I.6): the calm screens
 *    below are only shown while no player exists
 */
(function () {
  'use strict';

  var params = new URLSearchParams(window.location.search);
  var TOKEN = params.get('token') || '';
  var STATE_URL = 'player/api/state';
  var EVENT_URL = 'player/api/event';
  var YT_API_URL = 'https://www.youtube.com/iframe_api';
  var API_TIMEOUT_MS = 20000;
  var AUTOPLAY_CHECK_MS = 8000;
  var FADE_MS = 1500;
  var PS = { UNSTARTED: -1, ENDED: 0, PLAYING: 1, PAUSED: 2, BUFFERING: 3, CUED: 5 };
  var VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
  var LIST_ID_RE = /^[A-Za-z0-9_-]{2,64}$/;

  var st = {
    appliedSeq: null,
    action: 'idle',
    loadId: null,
    item: null,
    volume: 0.5,
    host: 'https://www.youtube-nocookie.com',
    pollMs: 500,
    heartbeatMs: 2000,
    player: null,
    playerReady: false,
    apiState: 'none', // none | loading | ready | failed
    apiWaiters: [],
    apiTimer: null,
    autoplayTimer: null,
    fadeTimer: null,
    screen: 'idle',
    authFailed: false,
    events: [] // last few events sent (debug / tests)
  };

  // ------------------------------------------------------------------ helpers
  function $(id) { return document.getElementById(id); }
  function clamp(v, lo, hi) { v = Number(v); if (!isFinite(v)) { v = lo; } return Math.max(lo, Math.min(hi, v)); }
  function safe(fn) { try { return fn(); } catch (e) { return undefined; } }

  function show(name) {
    st.screen = name;
    ['idle', 'loading', 'done', 'error'].forEach(function (n) {
      var el = $('screen-' + n);
      if (el) { el.classList.toggle('visible', n === name); }
    });
  }

  function post(type, extra) {
    var body = { type: type, load_id: st.loadId };
    if (extra) { Object.keys(extra).forEach(function (k) { if (extra[k] !== undefined) { body[k] = extra[k]; } }); }
    if (type !== 'heartbeat') {
      st.events.push(type + (body.code !== undefined && body.code !== null ? ':' + body.code : ''));
      if (st.events.length > 50) { st.events.shift(); }
    }
    return fetch(EVENT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Player-Token': TOKEN },
      body: JSON.stringify(body),
      cache: 'no-store',
      keepalive: true
    }).catch(function () { /* robot unreachable: the next poll/heartbeat retries */ });
  }

  function playerInfo() {
    var p = st.player;
    if (!p || !st.playerReady) { return {}; }
    var ps = safe(function () { return p.getPlayerState(); });
    var ct = safe(function () { return p.getCurrentTime(); });
    var du = safe(function () { return p.getDuration(); });
    return {
      player_state: typeof ps === 'number' ? ps : undefined,
      current_time: typeof ct === 'number' && ct >= 0 ? ct : undefined,
      duration: typeof du === 'number' && du >= 0 ? du : undefined,
      video_id: currentVideoId() || undefined
    };
  }

  function currentVideoId() {
    var p = st.player;
    if (!p) { return null; }
    var data = safe(function () { return p.getVideoData && p.getVideoData(); });
    if (data && typeof data.video_id === 'string' && VIDEO_ID_RE.test(data.video_id)) { return data.video_id; }
    var url = safe(function () { return p.getVideoUrl(); });
    if (typeof url === 'string') {
      var m = /[?&]v=([A-Za-z0-9_-]{11})/.exec(url);
      if (m) { return m[1]; }
    }
    return null;
  }

  // Is the video the player shows right now one the parent approved?
  function approvedNow() {
    var item = st.item;
    var vid = currentVideoId();
    if (!item || !vid) { return true; } // nothing to compare yet
    if (item.kind === 'video') { return vid === item.video_id; }
    var list = safe(function () { return st.player.getPlaylist(); });
    if (list && list.length) { return list.indexOf(vid) >= 0; }
    return vid === item.video_id || !item.video_id;
  }

  function playlistHasMore() {
    if (!st.item || st.item.kind !== 'playlist' || !st.player) { return false; }
    var list = safe(function () { return st.player.getPlaylist(); });
    var idx = safe(function () { return st.player.getPlaylistIndex(); });
    return !!(list && list.length && typeof idx === 'number' && idx >= 0 && idx < list.length - 1);
  }

  // ------------------------------------------------------------------ YouTube API loading
  function ensureApi(cb) {
    if (st.apiState === 'ready' && window.YT && window.YT.Player) { cb(true); return; }
    st.apiWaiters.push(cb);
    if (st.apiState === 'loading') { return; }
    st.apiState = 'loading';
    window.onYouTubeIframeAPIReady = function () { apiDone(true); };
    var old = $('yt-api-script');
    if (old && old.parentNode) { old.parentNode.removeChild(old); }
    var s = document.createElement('script');
    s.id = 'yt-api-script';
    s.src = YT_API_URL;
    s.async = true;
    s.onerror = function () { apiDone(false); };
    document.head.appendChild(s);
    st.apiTimer = setTimeout(function () { apiDone(false); }, API_TIMEOUT_MS);
  }

  function apiDone(ok) {
    if (st.apiState !== 'loading') { return; }
    clearTimeout(st.apiTimer);
    st.apiState = ok && window.YT && window.YT.Player ? 'ready' : 'failed';
    var waiters = st.apiWaiters;
    st.apiWaiters = [];
    waiters.forEach(function (fn) { fn(st.apiState === 'ready'); });
    if (st.apiState === 'failed') { st.apiState = 'none'; } // retry on the next load
  }

  // ------------------------------------------------------------------ player lifecycle
  function validItem(item) {
    if (!item) { return false; }
    if (item.kind === 'video') { return VIDEO_ID_RE.test(item.video_id || ''); }
    if (item.kind === 'playlist') {
      return LIST_ID_RE.test(item.list_id || '') && (!item.video_id || VIDEO_ID_RE.test(item.video_id));
    }
    return false;
  }

  function teardown() {
    clearTimeout(st.autoplayTimer);
    clearInterval(st.fadeTimer);
    st.fadeTimer = null;
    var p = st.player;
    st.player = null;
    st.playerReady = false;
    if (p) {
      safe(function () { p.stopVideo(); });
      safe(function () { p.destroy(); });
    }
    var host = $('yt-host');
    if (host) { host.innerHTML = ''; }
  }

  function loadItem(loadId, item) {
    teardown();
    st.loadId = loadId;
    st.item = item;
    if (!validItem(item)) { show('error'); post('error', { code: 'bad_item' }); return; }
    show('loading');
    ensureApi(function (ok) {
      if (st.loadId !== loadId) { return; } // superseded while the API loaded
      if (!ok) { show('error'); post('error', { code: 'api_load_failed' }); return; }
      if (st.action === 'stop' || st.action === 'idle') { return; }
      createPlayer(loadId, item);
    });
  }

  function createPlayer(loadId, item) {
    var host = $('yt-host');
    host.innerHTML = '';
    var el = document.createElement('div');
    el.id = 'yt-player';
    host.appendChild(el);
    show('video'); // from here on nothing covers the player (it shows its own loading state)
    var vars = {
      autoplay: 1,          // only ever the item the parent chose
      controls: 0,
      disablekb: 1,
      fs: 0,
      iv_load_policy: 3,    // no annotations
      rel: 0,               // related videos limited to the same channel (and we never show the end screen)
      playsinline: 1,
      modestbranding: 1,    // deprecated by YouTube, harmless
      cc_load_policy: 0,
      enablejsapi: 1,
      origin: window.location.origin,
      widget_referrer: window.location.origin
    };
    if (item.start_seconds > 0) { vars.start = Math.floor(item.start_seconds); }
    var opts = { width: '100%', height: '100%', host: st.host, playerVars: vars, events: {} };
    if (item.kind === 'playlist') {
      vars.listType = 'playlist';
      vars.list = item.list_id;
      if (item.video_id) { opts.videoId = item.video_id; }
    } else {
      opts.videoId = item.video_id;
    }
    opts.events.onReady = function () {
      if (st.loadId !== loadId) { return; }
      st.playerReady = true;
      applyVolume();
      if (st.action === 'play') { safe(function () { st.player.playVideo(); }); }
      else if (st.action === 'pause') { safe(function () { st.player.pauseVideo(); }); }
      post('ready', playerInfo());
    };
    opts.events.onStateChange = function (e) {
      if (st.loadId !== loadId) { return; }
      onStateChange(e && e.data);
    };
    opts.events.onError = function (e) {
      if (st.loadId !== loadId) { return; }
      var code = e && e.data;
      teardown();
      show('error');
      post('error', { code: typeof code === 'number' || typeof code === 'string' ? code : 'unknown' });
    };
    try {
      st.player = new window.YT.Player(el.id, opts);
    } catch (err) {
      st.player = null;
      show('error');
      post('error', { code: 'player_create_failed', detail: String(err).slice(0, 150) });
      return;
    }
    st.autoplayTimer = setTimeout(checkAutoplay, AUTOPLAY_CHECK_MS);
  }

  function onStateChange(s) {
    if (s === PS.PLAYING) {
      if (!approvedNow()) { reject(); return; }
      clearTimeout(st.autoplayTimer);
      post('playing', playerInfo());
      if (st.action === 'pause') { safe(function () { st.player.pauseVideo(); }); }
    } else if (s === PS.PAUSED) {
      post('paused', playerInfo());
    } else if (s === PS.BUFFERING) {
      post('buffering', playerInfo());
    } else if (s === PS.ENDED) {
      if (playlistHasMore()) { post('track_ended', playerInfo()); return; } // next video of the approved playlist
      finishEnded();
    }
  }

  function finishEnded() {
    teardown(); // destroy at once: no end screen, no related videos
    show('done');
    post('ended');
  }

  function reject() {
    var vid = currentVideoId();
    teardown();
    show('error');
    post('error', { code: 'unexpected_video', video_id: vid || undefined });
  }

  function checkAutoplay() {
    if (st.action !== 'play' || !st.player || !st.playerReady) { return; }
    var s = safe(function () { return st.player.getPlayerState(); });
    if (s === PS.UNSTARTED || s === PS.CUED) {
      // The browser refused autoplay (desktop preview without the kiosk flag). YouTube's own
      // play button is still there; the dashboard shows the hint.
      post('autoplay_blocked', playerInfo());
    }
  }

  function applyVolume() {
    var p = st.player;
    if (!p || !st.playerReady || st.fadeTimer) { return; }
    var v = Math.round(clamp(st.volume, 0, 1) * 100);
    if (v <= 0) { safe(function () { p.mute(); }); }
    else { safe(function () { p.unMute(); p.setVolume(v); }); }
  }

  function fadeOut(done) {
    var p = st.player;
    if (!p || !st.playerReady) { done(); return; }
    var start = Math.round(clamp(st.volume, 0, 1) * 100);
    var steps = 15;
    var i = 0;
    clearInterval(st.fadeTimer);
    st.fadeTimer = setInterval(function () {
      i += 1;
      safe(function () { p.setVolume(Math.max(0, Math.round(start * (1 - i / steps)))); });
      if (i >= steps) { clearInterval(st.fadeTimer); st.fadeTimer = null; done(); }
    }, FADE_MS / steps);
  }

  function finish(reason, hadItem) {
    if (reason === 'limit' && st.player) {
      fadeOut(function () { teardown(); show('done'); });
      return;
    }
    teardown();
    if (!hadItem) { show('idle'); return; }
    if (reason === 'ended' || reason === 'limit') { show('done'); }
    else if (reason === 'error') { show('error'); }
    else { show('idle'); }
  }

  // ------------------------------------------------------------------ desired state
  function apply(s) {
    if (!s || typeof s !== 'object') { return; }
    if (typeof s.poll_ms === 'number') { st.pollMs = clamp(s.poll_ms, 200, 5000); }
    if (typeof s.heartbeat_ms === 'number') { st.heartbeatMs = clamp(s.heartbeat_ms, 500, 10000); }
    if (typeof s.youtube_host === 'string' && /^https:\/\/www\.youtube(-nocookie)?\.com$/.test(s.youtube_host)) {
      st.host = s.youtube_host;
    }
    if (typeof s.volume === 'number' && s.volume !== st.volume) { st.volume = s.volume; applyVolume(); }
    if (s.seq === st.appliedSeq) { return; }
    var first = st.appliedSeq === null;
    st.appliedSeq = s.seq;
    var prevAction = st.action;
    st.action = s.action;
    if (s.action === 'play') {
      if (s.load_id && s.load_id !== st.loadId) { loadItem(s.load_id, s.item); }
      else if (st.player && st.playerReady && prevAction !== 'play') { safe(function () { st.player.playVideo(); }); }
    } else if (s.action === 'pause') {
      if (s.load_id && s.load_id !== st.loadId) { loadItem(s.load_id, s.item); }
      else if (st.player && st.playerReady) { safe(function () { st.player.pauseVideo(); }); }
    } else if (s.action === 'stop') {
      // A stop we see on first load belongs to an earlier session: just stay calm and blank.
      var hadItem = !first && st.loadId !== null && s.load_id === st.loadId;
      if (st.screen === 'done' && s.reason === 'ended') { teardown(); return; } // already handled locally
      finish(s.reason, hadItem);
    } else {
      teardown();
      if (st.screen !== 'done') { show('idle'); }
    }
  }

  function poll() {
    fetch(STATE_URL, { headers: { 'X-Player-Token': TOKEN }, cache: 'no-store' })
      .then(function (r) {
        if (r.status === 401 || r.status === 403) { st.authFailed = true; throw new Error('auth'); }
        if (!r.ok) { throw new Error('http ' + r.status); }
        return r.json();
      })
      .then(apply)
      .catch(function () {
        if (st.authFailed) {
          teardown();
          $('error-text').textContent = 'This player link is out of date. Open it again from the dashboard.';
          show('error');
        }
      })
      .then(function () {
        // After an auth failure keep checking slowly (the robot may have restarted with a new link).
        setTimeout(poll, st.authFailed ? 10000 : st.pollMs);
      });
  }

  function heartbeat() {
    if (!st.authFailed) {
      if (st.player && st.playerReady && st.screen === 'video' && !approvedNow()) { reject(); }
      else { post('heartbeat', playerInfo()); }
    }
    setTimeout(heartbeat, st.heartbeatMs);
  }

  // ------------------------------------------------------------------ boot
  // If anything covers or replaces this page while a video is loaded (a YouTube link opened a new
  // tab/window, the tab was switched, the page is being closed), tell the robot at once: it stops
  // the session and closes the kiosk browser, popups included. A hidden player must not keep playing.
  function onHidden(kind) {
    if (st.player && st.loadId && !st.authFailed) {
      teardown();
      show('idle');
      post(kind, { detail: kind === 'hidden' ? 'visibility hidden' : 'pagehide' });
    }
  }

  function boot() {
    document.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    document.addEventListener('keydown', function (e) { e.preventDefault(); }, true);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') { onHidden('hidden'); }
    });
    window.addEventListener('pagehide', function () { onHidden('navigated_away'); });
    show('idle');
    poll();
    setTimeout(heartbeat, st.heartbeatMs);
  }

  // Read-only debug view (no token) for tests and the browser console.
  window.__littlebotPlayer = {
    snapshot: function () {
      return {
        appliedSeq: st.appliedSeq, action: st.action, loadId: st.loadId, screen: st.screen,
        hasPlayer: !!st.player, playerReady: st.playerReady, volume: st.volume, apiState: st.apiState,
        events: st.events.slice()
      };
    }
  };

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); }
  else { boot(); }
})();
