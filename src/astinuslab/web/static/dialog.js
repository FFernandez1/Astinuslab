// Focus management for role="dialog"/aria-modal elements: focus-in, Tab trap,
// Escape to close, inert background, and restore focus to opener. Modals may
// stay nested in .shell: siblings along the path to BODY become inert.
(function () {
  var FOCUSABLE =
    "a[href],button:not([disabled]),input:not([disabled]),select:not([disabled])," +
    'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  var stack = [];

  function focusables(el) {
    return Array.prototype.filter.call(el.querySelectorAll(FOCUSABLE), function (node) {
      return node.offsetParent !== null || node === document.activeElement;
    });
  }

  function setOpen(el, openClass, open) {
    if (openClass) el.classList.toggle(openClass, open);
    else el.hidden = !open;
    el.setAttribute("aria-hidden", String(!open));
  }

  function inertBackground(el) {
    var changed = [];
    for (var branch = el; branch; branch = branch.parentElement) {
      // An outer dialog may have made this sibling dialog (or an ancestor)
      // inert. The top dialog needs an accessible path to the document; keep
      // every previous value in this stack frame so closing restores the parent.
      changed.push([branch, branch.inert]);
      branch.inert = false;
      if (!branch.parentElement) break;
      Array.from(branch.parentElement.children).forEach(function (sibling) {
        if (sibling === branch) return;
        // A live region opted out of inerting stays in the accessibility tree.
        // The toast root is a <body> sibling, so inerting it silenced every
        // error raised while a dialog was open.
        if (sibling.hasAttribute && sibling.hasAttribute("data-inert-exempt")) return;
        changed.push([sibling, sibling.inert]);
        sibling.inert = true;
      });
    }
    return changed;
  }

  function onFocus(event) {
    var current = stack[stack.length - 1];
    if (current && !current.el.contains(event.target)) {
      (focusables(current.el)[0] || current.el).focus();
    }
  }

  function rememberInvoker(node) {
    var scope = node && node.closest("[data-focus-scope]");
    var scopeKey = scope && scope.dataset.focusScope;
    var key = node && node.dataset.detailKey;
    return function () {
      var target = node;
      // Polling can replace an invoker while its dialog is open. Restore only
      // within the same selected entity, never to another VM's destructive action.
      if (target && !target.isConnected) {
        target = target.id ? document.getElementById(target.id) : null;
        if (!target && key && scope && scope.isConnected && scope.dataset.focusScope === scopeKey) {
          target = Array.from(scope.querySelectorAll("[data-detail-key]")).find(function (el) {
            return el.dataset.detailKey === key;
          });
        }
      }
      if (target && !target.disabled && typeof target.focus === "function") target.focus();
    };
  }

  function onKey(event) {
    var current = stack[stack.length - 1];
    if (!current) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopImmediatePropagation();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    var nodes = focusables(current.el);
    if (!nodes.length) {
      event.preventDefault();
      return;
    }
    var first = nodes[0];
    var last = nodes[nodes.length - 1];
    if (
      event.shiftKey &&
      (document.activeElement === first || !current.el.contains(document.activeElement))
    ) {
      event.preventDefault();
      last.focus();
    } else if (
      !event.shiftKey &&
      (document.activeElement === last || !current.el.contains(document.activeElement))
    ) {
      event.preventDefault();
      first.focus();
    }
  }

  function open(el, opts) {
    opts = opts || {};
    // A parent can repaint while its child is active. It already owns a stack
    // frame; reopening it must not move focus or replace the child's inert state.
    if (
      stack.some(function (entry) {
        return entry.el === el;
      })
    )
      return;
    var next = {
      el: el,
      openClass: opts.openClass || "",
      restoreFocus: rememberInvoker(opts.returnTo || document.activeElement),
      onClose: opts.onClose,
      inert: inertBackground(el),
    };
    stack.push(next);
    setOpen(el, next.openClass, true);
    var initial = (opts.initial && el.querySelector(opts.initial)) || focusables(el)[0] || el;
    if (!initial.hasAttribute("tabindex") && initial === el) initial.setAttribute("tabindex", "-1");
    initial.focus();
    if (stack.length === 1) {
      document.addEventListener("keydown", onKey, true);
      document.addEventListener("focusin", onFocus, true);
    }
  }

  function close() {
    if (!stack.length) return;
    var closing = stack.pop();
    if (!stack.length) {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("focusin", onFocus, true);
    }
    closing.inert.forEach(function (entry) {
      entry[0].inert = entry[1];
    });
    setOpen(closing.el, closing.openClass, false);
    if (closing.onClose) closing.onClose();
    closing.restoreFocus();
  }

  window.AlDialog = {
    open: open,
    close: close,
    get current() {
      var current = stack[stack.length - 1];
      return current && current.el;
    },
  };
})();
