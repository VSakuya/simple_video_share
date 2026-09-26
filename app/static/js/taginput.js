// taginput.js — shared tag chip input.
//
// A single reusable component rendered from the `_tag_input.html` partial and
// used by the upload queue page, the clip editor, and the My Videos edit
// dialog. It shows the selected tags as removable chips plus a trailing text
// field to add more. Typing an existing tag name (matched case-insensitively
// against the library carried in `data-tags`) selects it; a new name is created
// via POST /admin/tags/create. The 20-tag cap and duplicate detection are
// enforced here (and again server-side). Enter or comma commits the field;
// Backspace on an empty field removes the last chip.
//
// Exposes window.SVSTagInput = { init, all, get, set, reset }. Every `.tag-input`
// element is auto-initialised when this script runs (it is loaded after the
// page content, so the elements already exist); pages then call
// SVSTagInput.get(el) / .set(el, ids) on the element they rendered.

(function () {
  "use strict";

  const SVS_BASE = (typeof window !== "undefined" && window.SVS_BASE) ? window.SVS_BASE : "";

  function toast(message) {
    if (window.svsToast) window.svsToast(message, "error");
  }

  // One chip: a label, a remove button, and the hidden input that carries the
  // tag id (name="tags") for both native form submission and SVSTagInput.get().
  function buildChip(name, id) {
    const chip = document.createElement("span");
    chip.className = "tag-input__chip";
    const label = document.createElement("span");
    label.className = "tag-input__label";
    label.textContent = "#" + name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "tag-input__remove";
    remove.setAttribute("aria-label", "Remove tag " + name);
    remove.textContent = "\u00d7";
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "tags";
    hidden.value = String(id);
    remove.addEventListener("click", () => chip.remove());
    chip.append(label, remove, hidden);
    return chip;
  }

  function init(el) {
    if (!el || el.__svsTagInput) return el.__svsTagInput;

    const cap = Math.max(1, parseInt(el.getAttribute("data-max-tags"), 10) || 20);

    // The full tag library (id + name) from data-tags. Used to match an existing
    // name as it is typed and to look a name up by id when pre-filling via set().
    const byId = {};   // id (string) -> name
    const byName = {}; // lowercase name -> id (string)
    try {
      const raw = el.getAttribute("data-tags");
      const arr = raw ? JSON.parse(raw) : [];
      if (Array.isArray(arr)) {
        for (const t of arr) {
          if (t && t.id != null && t.name != null) {
            const id = String(t.id);
            byId[id] = String(t.name);
            byName[String(t.name).toLowerCase()] = id;
          }
        }
      }
    } catch (e) { /* malformed data-tags: start with an empty library */ }

    let field = el.querySelector(".tag-input__field");
    if (!field) {
      field = document.createElement("input");
      field.type = "text";
      field.className = "tag-input__field";
      field.placeholder = "Add a tag\u2026";
      field.maxLength = 32;
      field.autocomplete = "off";
      el.append(field);
    }

    // The currently selected tags, as { id, name } in selection order.
    let current = [];

    // Rebuild the chips (inserted before the field so it stays last) and drop
    // any stale ones.
    function render() {
      el.querySelectorAll(".tag-input__chip").forEach((c) => c.remove());
      for (const t of current) el.insertBefore(buildChip(t.name, t.id), field);
    }

    function addByName(raw) {
      const name = (raw || "").trim();
      field.value = ""; // clear now so a later blur cannot re-submit it
      if (!name) return;
      if (current.length >= cap) {
        toast("A video can have at most " + cap + " tags.");
        return;
      }
      const lc = name.toLowerCase();
      if (current.some((t) => t.name.toLowerCase() === lc)) {
        toast("That tag is already selected.");
        return;
      }
      if (Object.prototype.hasOwnProperty.call(byName, lc)) {
        // Exists in the library: select it without a round-trip.
        const id = byName[lc];
        current.push({ id, name: byId[id] });
        render();
        return;
      }
      // Not in the library: create it, then select.
      field.disabled = true;
      fetch(SVS_BASE + "/admin/tags/create", {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        body: new URLSearchParams({ name }),
      })
        .then((res) => res.json().then((j) => ({ res, j })))
        .then(({ res, j }) => {
          if (!res.ok || !j || j.ok !== true) throw new Error((j && j.error) || "Create failed");
          const id = String(j.tag_id);
          const createdName = j.name || name;
          byId[id] = createdName;
          byName[createdName.toLowerCase()] = id;
          current.push({ id, name: createdName });
          render();
        })
        .catch((e) => toast(e.message || "Could not create tag."))
        .finally(() => {
          field.disabled = false;
          field.focus();
        });
    }

    field.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        addByName(field.value);
      } else if (e.key === "Backspace" && field.value === "" && current.length) {
        current.pop();
        render();
      }
    });
    field.addEventListener("blur", () => {
      if (field.value.trim()) addByName(field.value);
    });

    render();

    el.__svsTagInput = {
      // The selected tag ids, as strings, in selection order.
      get: () => current.map((t) => t.id),
      // Select a set of ids (from the library); unknown/stale ids are dropped.
      set: (ids) => {
        const arr = Array.isArray(ids) ? ids : [];
        current = arr
          .map((id) => String(id))
          .filter((id) => Object.prototype.hasOwnProperty.call(byId, id))
          .map((id) => ({ id, name: byId[id] }));
        render();
      },
      reset: () => {
        current = [];
        field.value = "";
        render();
      },
    };
    return el.__svsTagInput;
  }

  function all() {
    return Array.from(document.querySelectorAll(".tag-input"));
  }

  function autoInit() {
    for (const el of all()) init(el);
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", autoInit);
    } else {
      autoInit();
    }
  }

  window.SVSTagInput = {
    init,
    all,
    get: (el) => (el && el.__svsTagInput ? el.__svsTagInput.get() : []),
    set: (el, ids) => { if (el && el.__svsTagInput) el.__svsTagInput.set(ids); },
    reset: (el) => { if (el && el.__svsTagInput) el.__svsTagInput.reset(); },
  };
})();
