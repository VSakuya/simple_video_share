// Live playback control shared by DPlayer's custom FLV integration.
(function (root, factory) {
  'use strict';
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SvsLivePlayback = api;
})(typeof window !== 'undefined' ? window : globalThis, function (root) {
  'use strict';

  var POLICY = {
    lowBuffer: 1, targetBuffer: 2, catchUpBuffer: 3,
    hardSeekBuffer: 10, hardSeekHeadroom: 1.5,
    sustainedMs: 3000, seekCooldownMs: 30000,
    maxRetries: 6, stableResetMs: 10000
  };

  function bufferState(video) {
    var state = { ahead: 0, start: null, end: null, range: -1, ranges: 0 };
    try {
      state.ranges = video.buffered.length;
      for (var i = 0; i < state.ranges; i++) {
        var start = video.buffered.start(i);
        var end = video.buffered.end(i);
        if (video.currentTime >= start && video.currentTime <= end) {
          state.start = start;
          state.end = end;
          state.range = i;
          state.ahead = Math.max(0, end - video.currentTime);
          break;
        }
      }
    } catch (e) { /* SourceBuffer can change while a source is detached. */ }
    return state;
  }

  function create(video, options) {
    options = options || {};
    var doc = root.document;
    var running = false;
    var disposed = false;
    var flv = null;
    var timer = null;
    var retryTimer = null;
    var pendingRetry = false;
    var retryCount = 0;
    var exhausted = false;
    var changingSource = false;
    var buffering = false;
    var catchingUp = false;
    var behindSince = null;
    var severeSince = null;
    var stableSince = null;
    var lastPosition = video.currentTime;
    var lastSeek = -Infinity;
    var lastSample = -Infinity;
    var events = [];
    var samples = [];
    var listeners = [];
    var statistics = {};
    var media = {};
    var counts = { waiting: 0, stalled: 0, seeks: 0, reconnects: 0, networkErrors: 0 };

    function now() { return root.performance.now(); }
    function hidden() { return !!doc.hidden; }
    function offline() { return root.navigator && root.navigator.onLine === false; }

    function snapshot() {
      var result = {
        at: Math.round(now()), utc: new Date().toISOString(), time: video.currentTime,
        buffer: bufferState(video), rate: video.playbackRate,
        paused: video.paused, readyState: video.readyState,
        hidden: hidden(), buffering: buffering
      };
      try {
        if (video.getVideoPlaybackQuality) {
          var quality = video.getVideoPlaybackQuality();
          result.decodedFrames = quality.totalVideoFrames;
          result.droppedFrames = quality.droppedVideoFrames;
        }
      } catch (e) { /* Diagnostics must not interrupt playback. */ }
      return result;
    }

    function record(type, detail) {
      var entry = { type: type, state: snapshot() };
      if (detail) entry.detail = detail;
      events.push(entry);
      if (events.length > 80) events.shift();
      if (options.debug && root.console) root.console.info('[live]', entry);
    }

    function setRate(rate) {
      if (video.playbackRate === rate) return;
      try {
        video.playbackRate = rate;
        record('rate', { value: rate });
      } catch (e) { /* Unsupported rate changes should leave playback usable. */ }
    }

    function resetChase() {
      catchingUp = false;
      behindSince = null;
      severeSince = null;
      setRate(1);
    }

    function clearRetry() {
      if (retryTimer !== null) root.clearTimeout(retryTimer);
      retryTimer = null;
    }

    function listen(target, name, handler) {
      target.addEventListener(name, handler);
      listeners.push(function () { target.removeEventListener(name, handler); });
    }

    function destroyFlv() {
      var old = flv;
      flv = null; // Ignore late callbacks from the previous instance.
      if (!old) return;
      changingSource = true;
      ['unload', 'detachMediaElement', 'destroy'].forEach(function (method) {
        try { old[method](); } catch (e) { /* Continue cleanup after partial failure. */ }
      });
      changingSource = false;
    }

    function notifyFatal(message) {
      clearRetry();
      pendingRetry = false;
      resetChase();
      destroyFlv();
      record('fatal');
      if (options.onFatalError) options.onFatalError(message);
    }

    function safePlay() {
      try {
        var promise = video.play();
        if (promise && promise.catch) promise.catch(function () {
          if (running) record('play-blocked');
        });
      } catch (e) { record('play-blocked'); }
    }

    function attachFlv(resume) {
      destroyFlv();
      buffering = false;
      stableSince = null;
      lastPosition = video.currentTime;
      media = {};
      var library = root.mpegts;
      if (!library || !library.isSupported()) {
        notifyFatal('Live playback is not supported by this browser.');
        return;
      }
      try {
        var active = library.createPlayer({ type: 'flv', url: options.url, isLive: true }, {
          enableStashBuffer: true,
          // Our controller owns catch-up. Do not run a second vendor chaser.
          liveBufferLatencyChasing: false,
          liveBufferLatencyChasingOnPaused: false,
          liveSync: false,
          lazyLoad: false,
          autoCleanupSourceBuffer: true,
          autoCleanupMaxBackwardDuration: 30,
          autoCleanupMinBackwardDuration: 15
        });
        flv = active;
        active.on(library.Events.ERROR, function (type, detail) {
          if (!running || flv !== active) return;
          resetChase();
          stableSince = null;
          // Never retain error info objects: they can contain stream URLs.
          var safeDetail = /^[A-Za-z0-9_]{1,80}$/.test(String(detail)) ? String(detail) : 'unknown';
          if (type === library.ErrorTypes.NETWORK_ERROR) {
            counts.networkErrors++;
            record('network-error', { code: safeDetail });
            pendingRetry = true;
            scheduleRetry();
          } else {
            record('media-error', { code: safeDetail });
            notifyFatal('Live codec or decoding is not supported. AV1 live playback requires an AV1-capable browser/device; use H.264 for compatibility.');
          }
        });
        active.on(library.Events.MEDIA_INFO, function (info) {
          if (!running || flv !== active) return;
          var previousMedia = JSON.stringify(media);
          ['videoCodec', 'audioCodec'].forEach(function (key) {
            if (/^[A-Za-z0-9._-]{1,80}$/.test(String(info[key] || ''))) media[key] = info[key];
          });
          ['width', 'height', 'fps'].forEach(function (key) {
            if (typeof info[key] === 'number' && isFinite(info[key])) media[key] = info[key];
          });
          if (JSON.stringify(media) !== previousMedia) record('media-info', Object.assign({}, media));
          var mse = root.ManagedMediaSource || root.MediaSource;
          if (media.videoCodec && media.videoCodec.indexOf('av01') === 0 &&
              mse && typeof mse.isTypeSupported === 'function' &&
              !mse.isTypeSupported('video/mp4; codecs="' + media.videoCodec + '"')) {
            notifyFatal('This browser/device cannot decode this AV1 live stream. Use an AV1-capable browser or ask the streamer to use H.264.');
          }
        });
        active.on(library.Events.STATISTICS_INFO, function (info) {
          if (!running || flv !== active) return;
          ['speed', 'decodedFrames', 'droppedFrames'].forEach(function (key) {
            if (typeof info[key] === 'number' && isFinite(info[key])) statistics[key] = info[key];
          });
        });
        changingSource = true;
        active.attachMediaElement(video);
        active.load();
        changingSource = false;
        if (resume) safePlay();
      } catch (e) {
        changingSource = false;
        destroyFlv();
        notifyFatal('The live player could not start. Reload the page to retry.');
      }
    }

    function scheduleRetry() {
      if (!running || !pendingRetry || retryTimer !== null) return;
      if (video.paused || hidden() || offline()) return;
      if (retryCount >= POLICY.maxRetries) {
        if (!exhausted) {
          exhausted = true;
          record('retry-exhausted');
          if (options.onFatalError) options.onFatalError('Live stream disconnected. Pause and play to retry.');
        }
        return;
      }
      var delay = Math.min(1000 * Math.pow(2, retryCount), 15000);
      retryTimer = root.setTimeout(function () {
        retryTimer = null;
        if (!running || video.paused || hidden() || offline()) return;
        pendingRetry = false;
        retryCount++;
        counts.reconnects++;
        record('reconnect', { attempt: retryCount });
        attachFlv(true);
      }, delay);
    }

    function tick() {
      if (!running) return;
      var time = now();
      var state = bufferState(video);
      if (time - lastSample >= 5000) {
        samples.push(snapshot());
        if (samples.length > 60) samples.shift();
        lastSample = time;
      }
      var playing = !video.paused && !video.seeking && !buffering && video.readyState >= 3;
      if (playing && !pendingRetry && video.currentTime > lastPosition) {
        if (stableSince === null) stableSince = time;
        if (time - stableSince >= POLICY.stableResetMs) {
          retryCount = 0;
          exhausted = false;
        }
      } else stableSince = null;
      lastPosition = video.currentTime;
      if (!playing || hidden() || pendingRetry || state.range < 0 || state.ahead < POLICY.lowBuffer) {
        resetChase();
        return;
      }
      if (state.ahead > POLICY.hardSeekBuffer) {
        if (severeSince === null) severeSince = time;
        if (time - severeSince >= POLICY.sustainedMs && time - lastSeek >= POLICY.seekCooldownMs) {
          lastSeek = time;
          try {
            var target = Math.max(state.start, state.end - POLICY.hardSeekHeadroom);
            video.currentTime = target;
            counts.seeks++;
            record('hard-seek', { target: target });
          } catch (e) { record('seek-failed'); }
          resetChase();
          return;
        }
      } else severeSince = null;
      if (state.ahead > POLICY.catchUpBuffer) {
        if (behindSince === null) behindSince = time;
        if (time - behindSince >= POLICY.sustainedMs) catchingUp = true;
      } else if (state.ahead <= POLICY.targetBuffer) {
        resetChase();
      } else if (!catchingUp) behindSince = null;
      setRate(catchingUp ? (state.ahead > 8 ? 1.25 : state.ahead > 5 ? 1.2 : 1.1) : 1);
    }

    function start() {
      if (running || disposed) return;
      running = true;
      listen(video, 'waiting', function () {
        buffering = true;
        counts.waiting++;
        resetChase();
        record('waiting');
      });
      listen(video, 'stalled', function () {
        counts.stalled++;
        resetChase();
        record('stalled');
      });
      listen(video, 'playing', function () { buffering = false; record('playing'); });
      listen(video, 'seeking', function () { resetChase(); record('seeking'); });
      listen(video, 'pause', function () {
        if (changingSource || !video.paused) return;
        clearRetry();
        resetChase();
        record('pause');
      });
      listen(video, 'play', function () {
        if (changingSource) return;
        if (exhausted) { retryCount = 0; exhausted = false; }
        scheduleRetry();
      });
      listen(doc, 'visibilitychange', function () {
        resetChase();
        if (hidden()) clearRetry();
        else scheduleRetry();
        record('visibility');
      });
      listen(root, 'offline', function () { clearRetry(); resetChase(); record('offline'); });
      listen(root, 'online', scheduleRetry);
      timer = root.setInterval(tick, 1000);
      attachFlv(false);
      record('start');
    }

    function destroy() {
      if (disposed) return;
      disposed = true;
      running = false;
      clearRetry();
      if (timer !== null) root.clearInterval(timer);
      timer = null;
      listeners.forEach(function (remove) { remove(); });
      listeners = [];
      resetChase();
      destroyFlv();
      record('destroy');
    }

    return {
      start: start,
      destroy: destroy,
      getDiagnostics: function () {
        return JSON.parse(JSON.stringify({
          active: running, retryAttempt: retryCount, policy: POLICY,
          backend: 'mpegts.js', media: media,
          counts: counts, current: snapshot(), statistics: statistics,
          samples: samples, events: events
        }));
      }
    };
  }

  return { create: create };
});
