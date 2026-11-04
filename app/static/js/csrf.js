// csrf.js - attach this page's CSRF token to every unsafe same-origin request.
//
// base.html publishes the session token as <meta name="csrf-token">. Patching
// fetch and XMLHttpRequest here means no individual call site has to remember
// the header (the server guard in app/csrf.py accepts X-CSRF-Token on POST,
// PUT, PATCH and DELETE). Native <form method="post"> posts are covered
// separately by the hidden _csrf field rendered as {{ csrf_field() }}, so this
// file deliberately does nothing for them.
//
// Two rules keep the token from leaking:
//   - only unsafe methods get it (a GET never needs it);
//   - only same-origin targets get it (an absolute URL to another host is
//     skipped, and a protocol-relative "//host/..." URL is treated as
//     cross-origin because the browser resolves it against another host).
(function () {
  'use strict';

  var HEADER = 'X-CSRF-Token';

  function token() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta && meta.getAttribute('content') ? meta.getAttribute('content') : '';
  }

  function isUnsafe(method) {
    var m = String(method || 'GET').toUpperCase();
    return m === 'POST' || m === 'PUT' || m === 'PATCH' || m === 'DELETE';
  }

  function sameOrigin(url) {
    var s = String(url === undefined || url === null ? '' : url);
    if (!s) return true;                       // no URL given: same document
    if (s.charAt(0) === '/' && s.charAt(1) !== '/') return true;  // app-relative
    if (s.charAt(0) === '#') return true;
    try {
      return new URL(s, window.location.href).origin === window.location.origin;
    } catch (e) {
      return false;                            // unparseable: do not attach
    }
  }

  // --- fetch ---------------------------------------------------------------
  var originalFetch = window.fetch;
  if (originalFetch) {
    window.fetch = function (input, init) {
      var url = typeof input === 'string' ? input
        : (input && input.url) ? input.url : '';
      var method = (init && init.method) || (input && input.method) || 'GET';
      if (isUnsafe(method) && sameOrigin(url)) {
        var next = init ? Object.assign({}, init) : {};
        var h = next.headers;
        if (window.Headers && h instanceof window.Headers) {
          h.append(HEADER, token());
        } else if (Array.isArray(h)) {
          next.headers = h.concat([[HEADER, token()]]);
        } else {
          next.headers = Object.assign({}, h || {});
          next.headers[HEADER] = token();
        }
        init = next;
      }
      return originalFetch.call(this, input, init);
    };
  }

  // --- XMLHttpRequest (used by the upload paths for real progress events) --
  if (window.XMLHttpRequest) {
    var originalOpen = XMLHttpRequest.prototype.open;
    var originalSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url) {
      this._svsMethod = method;
      this._svsUrl = url;
      return originalOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function () {
      if (isUnsafe(this._svsMethod) && sameOrigin(this._svsUrl)) {
        try {
          this.setRequestHeader(HEADER, token());
        } catch (e) { /* headers already sent: the server will reject */ }
      }
      return originalSend.apply(this, arguments);
    };
  }

  window.SVS_CSRF = { token: token, header: HEADER };
})();
