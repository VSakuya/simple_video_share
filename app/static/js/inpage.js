// inpage.js — page-level "fullscreen" overlay (no browser Fullscreen API).
//
// A single reusable helper that expands a player element (a native <video>)
// into a fixed, viewport-covering overlay. The element is MOVED into the
// overlay and moved back on exit. Esc or the on-screen exit button closes it.
// Loaded from base.html so any page can opt in with a [data-inpage-expand]
// button.
(function () {
  'use strict';

  var overlay = null;
  var stage = null;
  var el = null;      // element currently expanded
  var parent = null;  // original parent
  var next = null;    // original next sibling
  var active = false;

  function ensure() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'inpage-overlay';
    stage = document.createElement('div');
    stage.className = 'inpage-overlay__stage';
    var exitBtn = document.createElement('button');
    exitBtn.type = 'button';
    exitBtn.className = 'inpage-overlay__exit';
    exitBtn.setAttribute('aria-label', 'Exit in-page fullscreen');
    exitBtn.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
      'stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>';
    exitBtn.addEventListener('click', exit);

    overlay.appendChild(stage);
    overlay.appendChild(exitBtn);
    document.body.appendChild(overlay);
  }

  function expand(target) {
    if (active || !target) return;
    ensure();
    el = target;
    parent = el.parentNode;
    next = el.nextSibling;
    stage.appendChild(el);
    el.classList.add('inpage-overlay__fill');
    overlay.classList.add('is-active');
    active = true;

    // Auto-play the underlying <video> (if any) on entry.
    var v = el.tagName === 'VIDEO' ? el : (el.querySelector ? el.querySelector('video') : null);
    if (v) v.play().catch(function () {});
  }

  function exit() {
    if (!active) return;
    if (next && next.parentNode === parent) {
      parent.insertBefore(el, next);
    } else if (parent) {
      parent.appendChild(el);
    }
    el.classList.remove('inpage-overlay__fill');
    overlay.classList.remove('is-active');
    active = false;
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && active) exit();
  });

  // Any button carrying data-inpage-expand="<css selector>" expands that element.
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-inpage-expand]') : null;
    if (!btn) return;
    var target = document.querySelector(btn.getAttribute('data-inpage-expand'));
    if (target) expand(target);
  });

  window.inpage = {
    expand: expand,
    exit: exit,
    isActive: function () { return active; }
  };
})();
