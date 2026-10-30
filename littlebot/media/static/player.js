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
 *  - checks the video actually playing is the approved one, failing closed: a single video must have
 *    exactly the approved id; inside a playlist only ids of the approved playlist's own video list
 *    (read from the official player and reported to the robot) are accepted. While the id or the
 *    playlist's list isn't known yet the video may play for a short grace period only, then the
 *    page stops with unexpected_video. A missing id never counts as approval.
 *  - reports when the page is hidden (e.g. a YouTube link opened a new tab/window over it) or
 *    navigated away; the robot then stops and closes the whole kiosk browser
 *  - only one page plays: every page instance has a random page_id; the first page that polls a
 *    new load while visible claims it (state.owner). Other pages (e.g. a desktop tab left open
 *    after a stop) never create a player for it and stay on the calm idle screen; a page that
 *    loses the claim tears its player down. A hidden page never creates a player (it waits
 *    until it is visible again).
 *  - never draws anything over a live player and never blocks player functionality
 *    (YouTube Required Minimum Functionality / Developer Policies III.I.6): the calm screens
 *    below are only shown while no player exists
 *  - music (item.presentation === 'music', from the music skill): the player shrinks to a box in the
 *    corner (at least 200x200 px, still fully visible, nothing over it) and a now-playing screen fills
 *    the rest: the title, the channel, LittleBot's bobbing eyes and equaliser bars (pure CSS), resting
 *    while the player is paused. The video itself is never hidden or separated from its sound.
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
  var VERIFY_CHECK_MS = 250;   // re-check this often while a playing video can't be confirmed yet
  var MAX_PLAYLIST_IDS = 200;  // ids of one playlist we remember/report (the robot's cap too)
  var PAGE_ID = makePageId();  // this page instance (a reload is a new page)

  function makePageId() {
    var bytes = new Uint8Array(12);
    var c = window.crypto;
    if (c && typeof c.getRandomValues === 'function') { c.getRandomValues(bytes); }
    else { for (var i = 0; i < bytes.length; i++) { bytes[i] = Math.floor(Math.random() * 256); } }
    var hex = '';
    for (var j = 0; j < bytes.length; j++) { hex += (bytes[j] < 16 ? '0' : '') + bytes[j].toString(16); }
    return hex;
  }

  var st = {
    appliedSeq: null,
    action: 'idle',
    loadId: null,
    item: null,
    volume: 0.5,
    host: 'https://www.youtube-nocookie.com',
    pollMs: 500,
    heartbeatMs: 2000,
    verifyGraceMs: 4000, // from the robot (verify_grace_ms)
    player: null,
    playerReady: false,
    apiState: 'none', // none | loading | ready | failed
    apiWaiters: [],
    apiTimer: null,
    autoplayTimer: null,
    fadeTimer: null,
    started: false,       // the current load has reached PLAYING at least once
    confirmed: false,     // 'playing' was reported for the current PLAYING stretch
    pendingSince: null,   // when a playing video could not be confirmed yet (grace period)
    verifyTimer: null,
    playlistIds: null,    // video ids of the approved playlist, as read from the official player
    idsAcked: 0,          // how many of them the robot has confirmed receiving for this load
    deferred: null,       // {loadId, item}: player creation waiting for the page to become visible
    owner: null,          // page_id that owns the robot's current load (from the state)
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
    showMusic(name === 'video' && !!st.item && st.item.presentation === 'music');
  }

  // Music: the now-playing screen around the (smaller, never covered) player while it exists.
  function showMusic(on) {
    var body = document.body;
    if (!body) { return; }
    if (on) {
      var title = $('np-title');
      var channel = $('np-channel');
      if (title) { title.textContent = String(st.item.title || ''); }
      if (channel) { channel.textContent = String(st.item.channel || ''); }
    } else {
      body.classList.remove('paused');
    }
    body.classList.toggle('music', on);
  }

  function setPaused(paused) {
    if (document.body) { document.body.classList.toggle('paused', !!paused); }
  }

  function isHidden() { return document.visibilityState === 'hidden'; }

  function post(type, extra) {
    var body = { type: type, load_id: st.loadId, page_id: PAGE_ID };
    if (extra) { Object.keys(extra).forEach(function (k) { if (extra[k] !== undefined) { body[k] = extra[k]; } }); }
    if (type !== 'heartbeat') {
      st.events.push(type + (body.code !== undefined && body.code !== null ? ':' + body.code : ''));
      if (st.events.length > 50) { st.events.shift(); }
    }
    var sentIds = body.playlist_ids ? body.playlist_ids.length : 0;
    var loadId = st.loadId;
    return fetch(EVENT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Player-Token': TOKEN },
      body: JSON.stringify(body),
      cache: 'no-store',
      keepalive: true
    }).then(function (r) {
      // Keep re-sending the playlist's ids until the robot has them for this load.
      if (!sentIds || !r || !r.ok || typeof r.json !== 'function') { return; }
      return Promise.resolve(r.json()).then(function (j) {
        if (j && j.current === true && st.loadId === loadId && sentIds > st.idsAcked) { st.idsAcked = sentIds; }
      });
    }).catch(function () { /* robot unreachable: the next poll/heartbeat retries */ });
  }

  function playerInfo() {
    var p = st.player;
    if (!p || !st.playerReady) { return {}; }
    var ps = safe(function () { return p.getPlayerState(); });
    var ct = safe(function () { return p.getCurrentTime(); });
    var du = safe(function () { return p.getDuration(); });
    var info = {
      player_state: typeof ps === 'number' ? ps : undefined,
      current_time: typeof ct === 'number' && ct >= 0 ? ct : undefined,
      duration: typeof du === 'number' && du >= 0 ? du : undefined
    };
    // Only a video id we could check is reported (the robot checks it again); while it can't be
    // confirmed yet (grace period) it is left out rather than reported as if it were fine.
    if (verdict() !== 'pending') { info.video_id = currentVideoId() || undefined; }
    var ids = st.playlistIds;
    if (ids && ids.length > st.idsAcked) { info.playlist_ids = ids.slice(0, MAX_PLAYLIST_IDS); }
    return info;
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

  // Remember the video ids of the approved playlist as the official player reports them
  // (it was loaded with the approved list id only). Grows, never shrinks, capped.
  function refreshPlaylist() {
    var item = st.item;
    var p = st.player;
    if (!item || item.kind !== 'playlist' || !p) { return; }
    var list = safe(function () { return p.getPlaylist(); });
    if (!Array.isArray(list) || !list.length) { return; }
    var known = st.playlistIds ? st.playlistIds.slice() : [];
    for (var i = 0; i < list.length && known.length < MAX_PLAYLIST_IDS; i++) {
      var id = list[i];
      if (typeof id === 'string' && VIDEO_ID_RE.test(id) && known.indexOf(id) < 0) { known.push(id); }
    }
    if (known.length) { st.playlistIds = known; }
  }

  // Is the video in the player one the parent approved? 'ok' | 'pending' (can't tell yet: no video
  // id, or the playlist's list not loaded yet) | 'bad'. Fails closed: nothing unknown is 'ok'.
  function verdict() {
    var item = st.item;
    if (!item || !st.player) { return 'pending'; }
    if (item.kind === 'playlist') { refreshPlaylist(); }
    var vid = currentVideoId();
    if (!vid) { return 'pending'; }
    if (item.kind === 'video') { return vid === item.video_id ? 'ok' : 'bad'; }
    if (item.kind !== 'playlist') { return 'bad'; }
    if (!st.playlistIds) { return 'pending'; }
    return st.playlistIds.indexOf(vid) >= 0 ? 'ok' : 'bad';
  }

  // true: approved; false: rejected (torn down); 'pending': not confirmed yet. Once something has
  // played, an unconfirmed video gets st.verifyGraceMs, then it is rejected.
  function checkApproval() {
    if (!st.player) { return false; }
    var v = verdict();
    if (v === 'bad') { reject(); return false; }
    if (v === 'ok') { st.pendingSince = null; return true; }
    if (!st.started) { return 'pending'; } // nothing has played yet
    var now = Date.now();
    if (st.pendingSince === null) { st.pendingSince = now; }
    if (now - st.pendingSince >= st.verifyGraceMs) { reject(); return false; }
    scheduleVerify();
    return 'pending';
  }

  function scheduleVerify() {
    if (st.verifyTimer) { return; }
    st.verifyTimer = setTimeout(function () {
      st.verifyTimer = null;
      if (st.player && checkApproval() === true) { maybeConfirm(); }
    }, VERIFY_CHECK_MS);
  }

  // Report 'playing' for a video that was only confirmed after it started.
  function maybeConfirm() {
    if (st.confirmed || !st.player) { return; }
    if (safe(function () { return st.player.getPlayerState(); }) === PS.PLAYING) { confirmPlaying(); }
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
    clearTimeout(st.verifyTimer);
    st.verifyTimer = null;
    st.deferred = null;
    st.started = false;
    st.confirmed = false;
    st.pendingSince = null;
    st.playlistIds = null;
    st.idsAcked = 0;
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
    if (isHidden()) { st.deferred = { loadId: loadId, item: item }; return; } // wait until visible
    st.deferred = null;
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
    if (s !== PS.PLAYING) { st.confirmed = false; }
    if (s === PS.PLAYING || s === PS.PAUSED) { setPaused(s === PS.PAUSED); }
    if (s === PS.PLAYING) {
      st.started = true;
      st.confirmed = false;
      var ok = checkApproval();
      if (ok === false) { return; } // rejected and torn down
      if (st.action === 'pause') { safe(function () { st.player.pauseVideo(); }); }
      if (ok === true) { confirmPlaying(); } // else reported once confirmed (scheduleVerify)
    } else if (s === PS.PAUSED) {
      post('paused', playerInfo());
    } else if (s === PS.BUFFERING) {
      post('buffering', playerInfo());
    } else if (s === PS.ENDED) {
      if (playlistHasMore()) { post('track_ended', playerInfo()); return; } // next video of the approved playlist
      finishEnded();
    }
  }

  function confirmPlaying() {
    st.confirmed = true;
    clearTimeout(st.autoplayTimer);
    post('playing', playerInfo());
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
    if (typeof s.verify_grace_ms === 'number') { st.verifyGraceMs = clamp(s.verify_grace_ms, 200, 10000); }
    if (typeof s.youtube_host === 'string' && /^https:\/\/www\.youtube(-nocookie)?\.com$/.test(s.youtube_host)) {
      st.host = s.youtube_host;
    }
    if (typeof s.volume === 'number' && s.volume !== st.volume) { st.volume = s.volume; applyVolume(); }
    st.owner = typeof s.owner === 'string' ? s.owner : null;
    // (a robot that doesn't send "owner" at all predates page claims: its single page plays)
    var mine = st.owner === PAGE_ID || !Object.prototype.hasOwnProperty.call(s, 'owner');
    // Another page claimed the load this page holds: never keep a second player.
    if (st.owner && !mine && st.loadId !== null && s.load_id === st.loadId) { dropLoad(); }
    if (s.seq === st.appliedSeq) { return; }
    var first = st.appliedSeq === null;
    st.appliedSeq = s.seq;
    var prevAction = st.action;
    st.action = s.action;
    if (s.action === 'play' || s.action === 'pause') {
      if (s.load_id && s.load_id !== st.loadId) {
        // Only the page that claimed the load plays it (another page does, or this page is hidden
        // and hasn't claimed it yet - its next visible poll will).
        if (mine) { loadItem(s.load_id, s.item); } else { dropLoad(); }
      } else if (s.action === 'play') {
        if (st.player && st.playerReady && prevAction !== 'play') { safe(function () { st.player.playVideo(); }); }
      } else if (st.player && st.playerReady) {
        safe(function () { st.player.pauseVideo(); });
      }
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

  // Not (or no longer) this page's load: no player here, calm idle screen.
  function dropLoad() {
    var had = st.player || st.loadId !== null;
    teardown();
    st.loadId = null;
    st.item = null;
    if (had || st.screen === 'loading' || st.screen === 'video') { show('idle'); }
  }

  function poll() {
    var url = STATE_URL + '?page_id=' + PAGE_ID + '&visible=' + (isHidden() ? '0' : '1');
    fetch(url, { headers: { 'X-Player-Token': TOKEN }, cache: 'no-store' })
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
      var ok = st.player && st.playerReady && st.screen === 'video' ? checkApproval() : 'n/a';
      if (ok === true) { maybeConfirm(); }
      if (ok !== false) { post('heartbeat', playerInfo()); } // false: rejected (already reported)
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
      if (isHidden()) { onHidden('hidden'); return; }
      var d = st.deferred; // a claimed load whose player waited for the page to become visible
      if (d && d.loadId === st.loadId && !st.player && (st.action === 'play' || st.action === 'pause')) {
        createPlayer(d.loadId, d.item);
      }
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
        verifying: st.pendingSince !== null, playlistIds: st.playlistIds ? st.playlistIds.length : 0,
        idsAcked: st.idsAcked, pageId: PAGE_ID, owner: st.owner, deferred: !!st.deferred,
        music: !!(document.body && document.body.classList.contains('music')),
        events: st.events.slice()
      };
    }
  };

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); }
  else { boot(); }
})();
