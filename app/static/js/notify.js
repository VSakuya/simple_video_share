// notify.js — message notifications (§21), loaded from base.html.
//
// Owns the bottom-left user chip's red dot, its hover/click dropdown
// (Notifications / My Account), the recent-items dropdown panel, and the
// per-user SSE feed (GET /notify/stream). One EventSource per tab; the
// server pushes {"type":"notification","item":...} for new events and
// {"type":"unread-cleared"} after any tab marks everything read, so the dot
// stays in sync across the user's open tabs. The initial dot state is baked
// into the chip by base.html from the server-side unread count; this script
// only updates it afterwards. A no-op on guest pages (no #userchip element).
(function () {
  'use strict';

  var chip = document.getElementById('userchip');
  if (!chip) return;

  var dropdown = document.getElementById('userchip-dropdown');
  var panel = document.getElementById('notify-panel');
  var panelList = document.getElementById('notify-panel-list');
  var notifyItem = document.getElementById('userchip-notify-item');
  if (!dropdown || !panel || !panelList || !notifyItem) return;

  var base = window.SVS_BASE || '';
  var listUrl = base + '/notify/api/list?limit=20';
  var resyncUrl = base + '/notify/api/list?limit=1';
  var readUrl = base + '/notify/api/read';
  var streamUrl = base + '/notify/stream';
  var CLOSE_DELAY = 180; // ms of grace so the pointer can cross into the menu
  var MAX_PANEL_ROWS = 30;

  var dropdownOpen = false;
  var panelOpen = false;
  var closeTimer = null;
  var canHover = window.matchMedia &&
    window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  // --- red dot state -------------------------------------------------------
  // Toggles the dot on the avatar chip and on the "Notifications" dropdown
  // entry (the initial baked state comes from base.html).
  function setDot(unread) {
    chip.classList.toggle('is-unread', !!unread);
    var itemDot = notifyItem.querySelector('.userchip-dropdown__dot');
    if (itemDot) itemDot.hidden = !unread;
  }

  // --- positioning ----------------------------------------------------------
  // Both overlays are position:fixed on <body> and anchored to the chip's
  // current rect: to its right, bottom-aligned to the chip (the chip sits at
  // the sidebar's bottom, so the menu stays fully on-screen), clamped to the
  // viewport. Works in the expanded sidebar, the collapsed icon rail, and the
  // mobile drawer alike.
  function place(el, anchor) {
    var r = anchor.getBoundingClientRect();
    var w = el.offsetWidth;
    var h = el.offsetHeight;
    var left = r.right + 8;
    if (left + w > window.innerWidth - 8) {
      left = Math.max(8, r.left - w - 8); // no room on the right: go left
    }
    var top = Math.min(r.bottom - h, window.innerHeight - h - 8);
    top = Math.max(8, top);
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  }

  // --- dropdown (Notifications / My Account) --------------------------------
  function openDropdown() {
    if (panelOpen) closePanel();
    clearTimeout(closeTimer);
    dropdownOpen = true;
    dropdown.hidden = false;
    place(dropdown, chip);
  }
  function closeDropdown() {
    clearTimeout(closeTimer);
    if (!dropdownOpen) return;
    dropdownOpen = false;
    dropdown.hidden = true;
  }
  function scheduleClose() {
    clearTimeout(closeTimer);
    closeTimer = setTimeout(closeDropdown, CLOSE_DELAY);
  }

  if (canHover) {
    chip.addEventListener('mouseenter', openDropdown);
    chip.addEventListener('mouseleave', scheduleClose);
    dropdown.addEventListener('mouseenter', function () { clearTimeout(closeTimer); });
    dropdown.addEventListener('mouseleave', scheduleClose);
  }
  chip.addEventListener('click', function (ev) {
    ev.stopPropagation();
    if (panelOpen) { closePanel(); return; }
    if (dropdownOpen) closeDropdown();
    else openDropdown();
  });
  chip.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      if (dropdownOpen) closeDropdown();
      else openDropdown();
    }
  });

  // --- recent-items panel ---------------------------------------------------
  // Per-type one-line wording ("X commented on "Title"" / "X replied to your
  // comment on "Title""); an unknown type degrades to "notified you".
  function verb(type) {
    if (type === 'comment_reply') return 'replied to your comment on';
    if (type === 'video_comment') return 'commented on';
    return 'notified you';
  }
  // Same wording for the SSE toast.
  function lineText(it) {
    var s = (it.actor_username || 'Someone') + ' ' + verb(it.type);
    if (it.video_title) s += ' "' + it.video_title + '"';
    return s;
  }
  // /watch/<id>#c<comment_id> deep link (null when the item has no video).
  function rowHref(it) {
    if (!it.video_id) return '';
    var h = base + '/watch/' + it.video_id;
    if (it.comment_id) h += '#c' + it.comment_id;
    return h;
  }
  function localTime(utc) {
    var d = new Date(String(utc).replace(' ', 'T') + 'Z');
    return isNaN(d) ? '' : d.toLocaleString(undefined, { hour12: false });
  }

  // Build one row element (the /notifications page renders the identical
  // markup server-side with the same classes). data-href holds the full
  // mount-prefixed URL (rowHref includes SVS_BASE), so the panel's click
  // handler can navigate with it as-is.
  function makeRow(it) {
    var row = document.createElement('div');
    var href = rowHref(it);
    row.className = 'notify-row' + (it.unread ? ' notify-row--unread' : '') +
      (href ? ' notify-row--link' : '');
    if (href) row.setAttribute('data-href', href);
    var img = document.createElement('img');
    img.className = 'notify-row__avatar';
    img.src = it.actor_avatar_url || (base + '/static/img/avatar-default.svg');
    img.alt = '';
    img.loading = 'lazy';
    var main = document.createElement('div');
    main.className = 'notify-row__main';
    var text = document.createElement('div');
    text.className = 'notify-row__text';
    var actor = document.createElement('span');
    actor.className = 'notify-row__actor';
    actor.textContent = it.actor_username || 'Someone';
    text.appendChild(actor);
    text.appendChild(document.createTextNode(' ' + verb(it.type) + ' '));
    if (it.video_title) {
      var title = document.createElement('span');
      title.className = 'notify-row__title';
      title.textContent = '\u201C' + it.video_title + '\u201D';
      text.appendChild(title);
    }
    main.appendChild(text);
    var time = document.createElement('div');
    time.className = 'notify-row__time muted';
    if (it.created_at) {
      time.setAttribute('data-ts', it.created_at);
      time.textContent = localTime(it.created_at) || it.created_at;
    } else {
      time.textContent = 'just now';
    }
    main.appendChild(time);
    row.appendChild(img);
    row.appendChild(main);
    return row;
  }

  function trimPanel() {
    var rows = panelList.querySelectorAll('.notify-row');
    while (rows.length > MAX_PANEL_ROWS) {
      panelList.removeChild(rows[rows.length - 1]);
      rows = panelList.querySelectorAll('.notify-row');
    }
  }

  // The panel fetches the recent list first (so the rows keep their unread
  // markers as of the fetch), then marks everything read: the dot drops
  // immediately on this tab (optimistic) and the POST's unread-cleared SSE
  // event takes care of the user's other tabs.
  function openPanel() {
    closeDropdown();
    panelOpen = true;
    panel.hidden = false;
    place(panel, chip);
    setDot(false);
    panelList.innerHTML = '';
    fetch(listUrl)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) return;
        var items = j.items || [];
        if (!items.length) {
          var e = document.createElement('div');
          e.className = 'notify-panel__empty muted';
          e.textContent = 'No notifications yet.';
          panelList.appendChild(e);
          return;
        }
        for (var i = items.length - 1; i >= 0; i--) {
          panelList.insertBefore(makeRow(items[i]), panelList.firstChild);
        }
        trimPanel();
      })
      .catch(function () { /* transient: the panel shows its last state */ });
    fetch(readUrl, { method: 'POST' })
      .then(function (r) { return r.json(); })
      .catch(function () { /* the /notifications page path also marks read */ });
  }
  function closePanel() {
    if (!panelOpen) return;
    panelOpen = false;
    panel.hidden = true;
  }

  notifyItem.addEventListener('click', openPanel);
  dropdown.addEventListener('click', function (ev) {
    // "My Account" is a real link: close the menus + the mobile drawer, then
    // let the browser navigate.
    if (ev.target.closest && ev.target.closest('a')) {
      closeDropdown();
      closePanel();
      document.body.classList.remove('sidebar-open');
    }
  });

  // Row click → deep link to the comment (panel rows are built by makeRow).
  panelList.addEventListener('click', function (ev) {
    var row = ev.target.closest ? ev.target.closest('.notify-row--link') : null;
    if (!row) return;
    window.location.href = row.getAttribute('data-href');
  });

  // Close on outside click / Esc (covers dropdown + panel).
  document.addEventListener('click', function (ev) {
    var t = ev.target;
    if (dropdownOpen && !chip.contains(t) && !dropdown.contains(t)) closeDropdown();
    if (panelOpen && !chip.contains(t) && !panel.contains(t) && !dropdown.contains(t)) {
      closePanel();
    }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { closeDropdown(); closePanel(); }
  });
  window.addEventListener('resize', function () {
    if (dropdownOpen) place(dropdown, chip);
    if (panelOpen) place(panel, chip);
  });

  // --- per-user SSE feed ----------------------------------------------------
  // notification → light the dot (+ prepend to the open panel + a subtle
  // toast); unread-cleared → drop the dot (keeps this tab in sync when
  // another tab marked everything read). EventSource reconnects on its own.
  if (window.EventSource) {
    var es = new EventSource(streamUrl);
    // Resync the dot on every (re)establishment of the feed: 'notification'
    // events broadcast during a disconnect window (server restart, network
    // blip) are never replayed, so after the initial connect and after each
    // automatic reconnect the unread count is re-fetched (limit=1 keeps the
    // payload tiny) and the dot is aligned to it — one GET per reconnect,
    // no polling. A full page render already bakes the correct initial state,
    // so a transient fetch failure simply leaves the dot as it is.
    es.addEventListener('open', function () {
      fetch(resyncUrl)
        .then(function (r) { return r.json(); })
        .then(function (j) { if (j.ok) setDot(!!j.unread); })
        .catch(function () { /* transient: the current dot state stands */ });
    });
    es.addEventListener('message', function (e) {
      var data;
      try { data = JSON.parse(e.data); } catch (err) { return; }
      if (data.type === 'notification') {
        setDot(true);
        if (window.svsToast) window.svsToast(lineText(data.item), 'info');
        if (panelOpen) {
          var empty = panelList.querySelector('.notify-panel__empty');
          if (empty) empty.remove();
          panelList.insertBefore(makeRow(data.item), panelList.firstChild);
          trimPanel();
        }
      } else if (data.type === 'unread-cleared') {
        setDot(false);
        var rows = panelList.querySelectorAll('.notify-row--unread');
        for (var i = 0; i < rows.length; i++) {
          rows[i].classList.remove('notify-row--unread');
        }
      }
    });
    es.addEventListener('error', function () { /* reconnect is automatic */ });
  }

  // --- server-rendered rows (the /notifications page) ------------------------
  // The page renders the same row markup; bind the deep-link clicks and render
  // the UTC timestamps in the viewer's local timezone (same convention as the
  // watch page's comment times).
  var pageRows = document.querySelectorAll('.notify-row--link');
  if (pageRows.length) {
    pageRows.forEach(function (row) {
      row.addEventListener('click', function () {
        var href = row.getAttribute('data-href');
        if (href) window.location.href = base + href;
      });
    });
    (function refreshTimes() {
      document.querySelectorAll('.notify-row__time[data-ts]').forEach(function (el) {
        var t = localTime(el.getAttribute('data-ts'));
        if (t) el.textContent = t;
      });
    })();
  }
})();