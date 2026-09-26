// async.js — generic no-reload form handler + toast (§16.1). Loaded from
// base.html after the confirm script so window.svsConfirm is available.
//
// Any <form data-async> is intercepted: its submit is prevented, the existing
// data-confirm dialog is shown (via window.svsConfirm) when present, the form
// is POSTed to its own action with X-Requested-With: XMLHttpRequest, and the
// JSON response drives a toast + a declarative DOM update — no page reload.
// base.html's confirm handler skips form[data-async] so this owns async forms'
// confirm+fetch (no double dialog).
(function () {
  'use strict';

  // ---- Toast -------------------------------------------------------------
  var toastsEl = null;
  function ensureToasts() {
    if (toastsEl) return toastsEl;
    toastsEl = document.getElementById('svs-toasts');
    if (!toastsEl) {
      toastsEl = document.createElement('div');
      toastsEl.id = 'svs-toasts';
      toastsEl.setAttribute('aria-live', 'polite');
      document.body.appendChild(toastsEl);
    }
    return toastsEl;
  }

  // svsToast(message, type): a self-dismissing toast. 'error' styles it red;
  // anything else (default) is neutral. Returns the toast element.
  window.svsToast = function (message, type) {
    var host = ensureToasts();
    var el = document.createElement('div');
    el.className = 'svs-toast' + (type === 'error' ? ' svs-toast--error' : '');
    el.textContent = message;
    host.appendChild(el);
    void el.offsetWidth; // flush so the entrance transition runs
    el.classList.add('is-shown');
    setTimeout(function () {
      el.classList.remove('is-shown');
      setTimeout(function () { el.remove(); }, 250);
    }, 3500);
    return el;
  };

  // Page-specific success actions, keyed by data-async-action. Pages register
  // their own (e.g. the admin pin toggle); the generic helper just dispatches.
  window.svsAsyncActions = window.svsAsyncActions || {};

  function applyUpdate(form, data) {
    var removeSel = form.getAttribute('data-async-remove');
    if (removeSel) {
      var target = form.closest(removeSel);
      if (target) target.remove();
      return;
    }
    var addSel = form.getAttribute('data-async-add');
    if (addSel && data && data.html) {
      var host = document.querySelector(addSel);
      if (host) host.insertAdjacentHTML('beforeend', data.html);
      return;
    }
    var refetchSel = form.getAttribute('data-async-refetch');
    if (refetchSel) {
      var url = form.getAttribute('data-async-refetch-url');
      if (url) {
        fetch(url).then(function (r) { return r.text(); }).then(function (html) {
          var box = document.querySelector(refetchSel);
          if (box) box.innerHTML = html;
        }).catch(function () { window.svsToast('Could not refresh.', 'error'); });
      }
      return;
    }
    var action = form.getAttribute('data-async-action');
    if (action && window.svsAsyncActions[action]) {
      window.svsAsyncActions[action](form, data);
    }
  }

  function setBusy(form, busy) {
    var btn = form.querySelector('button[type="submit"], input[type="submit"]');
    if (!btn) return;
    if (busy) {
      btn.disabled = true;
      if (btn.tagName === 'BUTTON') {
        if (!btn.dataset._label) btn.dataset._label = btn.textContent;
        btn.textContent = btn.dataset._label + '…';
      }
    } else {
      btn.disabled = false;
      if (btn.tagName === 'BUTTON' && btn.dataset._label) {
        btn.textContent = btn.dataset._label;
        delete btn.dataset._label;
      }
    }
  }

  function submitForm(form) {
    var body = new FormData(form);
    setBusy(form, true);
    fetch(form.action, {
      method: (form.method || 'POST').toUpperCase(),
      body: body,
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }).then(function (res) {
      return res.json().then(function (j) { return { ok: res.ok, j: j }; })
        .catch(function () { return { ok: res.ok, j: {} }; });
    }).then(function (result) {
      setBusy(form, false);
      var j = result.j;
      if (!result.ok || j.ok === false) {
        window.svsToast((j && j.error) || 'Something went wrong.', 'error');
        return;
      }
      if (j.message) window.svsToast(j.message, 'success');
      applyUpdate(form, j);
    }).catch(function (e) {
      setBusy(form, false);
      window.svsToast('Request failed: ' + e, 'error');
    });
  }

  // Delegation: any form[data-async] (including rows added later, e.g. a newly
  // created user) is handled here. base.html's per-form confirm listener skips
  // data-async forms, so this owns their confirm+fetch.
  document.addEventListener('submit', function (ev) {
    var form = ev.target && ev.target.closest ? ev.target.closest('form[data-async]') : null;
    if (!form) return;
    ev.preventDefault();
    var confirmMsg = form.getAttribute('data-confirm');
    if (confirmMsg && form.dataset._confirming !== '1') {
      window.svsConfirm(confirmMsg).then(function (ok) {
        if (ok) submitForm(form);
      });
    } else {
      submitForm(form);
    }
  });
})();
