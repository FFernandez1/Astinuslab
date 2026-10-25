// Minimal DOM boundary: replaceChildren uses DOMString conversion, including null.
export class Node {
  constructor(tag = "div", text = "") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = new Map();
    this.parentNode = null;
    this._text = text;
    this.checked = false;
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }
  appendChild(child) {
    if (!(child instanceof Node)) throw new TypeError("appendChild requires a node");
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  replaceChildren(...children) {
    this.children.forEach((child) => (child.parentNode = null));
    this.children = [];
    this._text = "";
    for (const child of children)
      this.appendChild(child instanceof Node ? child : new Node("#text", String(child)));
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key.startsWith("data-"))
      this.dataset[key.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] =
        String(value);
  }
  getAttribute(key) {
    return this.attributes[key] ?? null;
  }
  // The page toggles a selected state on the file picker. The double exposes
  // only what the page uses; toggle's force argument is the one form it calls.
  get classList() {
    const owner = this;
    const names = () =>
      String(owner.className || "")
        .split(/\s+/)
        .filter(Boolean);
    const write = (list) => {
      owner.className = list.join(" ");
    };
    return {
      contains: (name) => names().includes(name),
      add(name) {
        const list = names();
        if (!list.includes(name)) write([...list, name]);
      },
      remove(name) {
        write(names().filter((item) => item !== name));
      },
      toggle(name, force) {
        const present = names().includes(name);
        const wanted = force === undefined ? !present : Boolean(force);
        if (wanted) this.add(name);
        else this.remove(name);
        return wanted;
      },
    };
  }
  click() {
    this.clicks = (this.clicks || 0) + 1;
    // A browser only downloads for an anchor that is in the document.
    this.connectedAtClick = Boolean(this.parentNode);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }
  addEventListener(type, callback) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), callback]);
  }
  matches(selector) {
    const match = selector.match(/^(\w+)?(?:\[([^=\]]+)(?:=["']?([^\]"']*)["']?)?\])?$/);
    if (!match) throw new Error(`Unsupported selector: ${selector}`);
    const [, tag, key, value] = match;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    if (!key) return true;
    const actual = key.startsWith("data-")
      ? this.dataset[key.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())]
      : (this.attributes[key] ?? this[key]);
    return actual !== undefined && (value === undefined || String(actual) === value);
  }
  closest(selector) {
    return this.matches(selector) ? this : this.parentNode?.closest(selector) || null;
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(selector.split(/,\s*/).some((part) => child.matches(part)) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  async fire(type, target = this) {
    const event = { type, target, currentTarget: this, preventDefault() {} };
    await Promise.all((this.listeners.get(type) || []).map((callback) => callback(event)));
  }
}

export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
export async function flush() {
  // Drain promise chains (fetch, response parsing, actions, poller callbacks), no real timers.
  for (let i = 0; i < 40; i++) await Promise.resolve();
}
export function response(value, status = 200, { blob, headers } = {}) {
  return {
    ok: status < 400,
    status,
    text: async () => JSON.stringify(value),
    blob: async () => blob ?? value,
    headers: { get: (name) => headers?.[name.toLowerCase()] ?? null },
  };
}
export function install(t) {
  const saved = new Map();
  const put = (key, value) => {
    saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  };
  const ids = Object.fromEntries(
    [
      "packages-root",
      "package-error",
      "package-status-message",
      "package-inspection",
      "package-library",
      "package-detail",
      "package-use-tab",
      "package-create-tab",
      "package-recipient-panel",
      "package-creator-panel",
      "package-action-status",
      "package-prepare-status",
      "package-operation-status",
      "verification-status",
      "verification-attempt",
      "verification-history",
      "package-inspect-form",
      "creator-verify-form",
      "prepare-network",
      "package-file",
      "package-file-name",
      "package-file-drop",
    ].map((id) => [id, new Node()]),
  );
  ids["creator-verify-form"].elements = { namedItem: () => null };
  const body = new Node("body");
  const created = [];
  put("document", {
    readyState: "complete",
    cookie: "",
    body,
    createElement: (tag) => {
      const node = new Node(tag);
      created.push(node);
      return node;
    },
    getElementById: (id) => ids[id] || null,
    querySelector: () => null,
  });
  // Object URLs are a resource the page must release, so the harness records
  // creation and revocation instead of pretending both always succeed.
  const objectUrls = [];
  put(
    "URL",
    Object.assign(class extends globalThis.URL {}, {
      createObjectURL(blob) {
        const url = `blob:astinuslab/${objectUrls.length}`;
        objectUrls.push({ url, blob, revoked: false });
        return url;
      },
      revokeObjectURL(url) {
        const entry = objectUrls.find((item) => item.url === url);
        if (entry) entry.revoked = true;
      },
    }),
  );
  put("window", { t: (key, fallback) => fallback || key, confirmDialog: async () => true });
  put("navigator", { clipboard: { writeText: async () => {} } });
  const requests = [];
  put("fetch", (url, options) => {
    const pending = deferred();
    requests.push({ path: url.replace("/api/v1/lab-packages", ""), ...options, ...pending });
    return pending.promise;
  });
  let now = 0,
    next = 0;
  const timers = new Map();
  put("setTimeout", (callback, delay) => {
    timers.set(++next, { callback, due: now + delay });
    return next;
  });
  put("clearTimeout", (id) => timers.delete(id));
  put("setInterval", (callback, delay) => {
    timers.set(++next, { callback, due: now + delay, interval: delay });
    return next;
  });
  put("clearInterval", (id) => timers.delete(id));
  t.after(() => {
    for (const [key, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return {
    ids,
    requests,
    timers,
    body,
    objectUrls,
    anchors: () => created.filter((node) => node.tagName === "A"),
    take(path, method = "GET") {
      const found = requests.find(
        (item) => !item.taken && item.path === path && item.method === method,
      );
      if (!found)
        throw new Error(
          `No ${method} ${path}; got ${requests.map((r) => `${r.method} ${r.path}`)}`,
        );
      found.taken = true;
      return found;
    },
    async advance(ms) {
      now += ms;
      for (const [id, timer] of [...timers]) {
        if (timer.due > now) continue;
        if (timer.interval) timer.due = now + timer.interval;
        else timers.delete(id);
        timer.callback();
      }
      await flush();
    },
    async click(panel, selector) {
      const node = ids[panel].querySelector(selector);
      if (!node) throw new Error(`Missing ${selector} in ${ids[panel].textContent}`);
      // DOM bubbling dispatches on target and ancestors without awaiting async handlers.
      for (let current = node; current; current = current.parentNode)
        void current.fire("click", node);
      await flush();
    },
  };
}
