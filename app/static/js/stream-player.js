// Shared player-source picker for the watch page and the Watch Together page
// (31.10). One rule: use HLS only when the server has a validated stream AND
// this browser can be trusted with it; otherwise keep today's progressive
// /watch/<id>/stream URL, which is what has always worked here.
//
// Why a helper instead of just passing the playlist: DPlayer decides how to
// attach a source from options.video.type. "hls" makes it build MSE through
// window.Hls (hls.js); "video"/"normal" just sets video.src. It also rewrites
// "hls" to "normal" when the element itself reports canPlayType(mpegURL), which
// is exactly what Safari wants. So the picker must know about hls.js BEFORE
// DPlayer is constructed - hence this file plus vendor/hls/hls.min.js loading
// before the player script in both templates.
(function () {
  'use strict';

  function nativeHls() {
    // Safari / iOS play HLS inside the element. DPlayer tests the same two MIME
    // strings, so asking the same way keeps the two decisions in step.
    try {
      var el = document.createElement('video');
      return !!(el.canPlayType('application/vnd.apple.mpegurl') ||
                el.canPlayType('application/x-mpegURL'));
    } catch (err) {
      return false;
    }
  }

  function hlsJsReady() {
    var Hls = window.Hls;
    return !!(Hls && typeof Hls.isSupported === 'function' && Hls.isSupported());
  }

  window.SVS_STREAM = {
    // pick({progressiveUrl, hlsUrl, streamState}) -> {url, type, hlsJs}
    pick: function (opts) {
      var progressive = {
        url: (opts && opts.progressiveUrl) || '',
        type: 'video',
        hlsJs: false
      };
      if (!opts || opts.streamState !== 'ready' || !opts.hlsUrl) return progressive;
      if (hlsJsReady()) return { url: opts.hlsUrl, type: 'hls', hlsJs: true };
      if (nativeHls()) return { url: opts.hlsUrl, type: 'video', hlsJs: false };
      return progressive;
    },

    usesHlsJs: function (picked) {
      return !!(picked && picked.hlsJs);
    },

    // Minimal self-healing for a picked HLS source (31.6). Network and media
    // errors are recoverable in place; anything else means the stream tree is
    // broken, and an admin fixes that with "Rebuild stream" (32.3) - retrying it
    // silently would just spin.
    attachRecovery: function (player, context) {
      var Hls = window.Hls;
      if (!player || !Hls || !player.plugins || !player.plugins.hls) return;
      var hls = player.plugins.hls;
      hls.on(Hls.Events.ERROR, function (event, data) {
        if (!data || !data.fatal) return;
        var where = (context && context.label) || 'hls';
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          console.warn('[svs] ' + where + ': fatal network error, reloading');
          hls.startLoad();
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          console.warn('[svs] ' + where + ': fatal media error, asking hls.js to recover');
          hls.recoverMediaError();
        } else {
          console.error('[svs] ' + where + ': unrecoverable hls error (' + data.details +
            '); the stream needs an admin rebuild');
        }
      });
    }
  };
})();
