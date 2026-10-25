export function $(id) {
  return document.getElementById(id);
}

export function el(tag, opts = {}) {
  const node = document.createElement(tag);
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.className) node.className = opts.className;
  if (opts.attrs)
    for (const [k, v] of Object.entries(opts.attrs)) {
      if (v === false || v == null || k.toLowerCase().startsWith("on")) continue;
      if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, String(v));
    }
  if (opts.dataset) for (const [k, v] of Object.entries(opts.dataset)) node.dataset[k] = String(v);
  if (opts.style) for (const [k, v] of Object.entries(opts.style)) node.style.setProperty(k, v);
  if (opts.children)
    for (const c of opts.children) {
      if (c != null) node.appendChild(c);
    }
  if (opts.onClick) node.addEventListener("click", opts.onClick);
  if (opts.onInput) node.addEventListener("input", opts.onInput);
  if (opts.onKeyDown) node.addEventListener("keydown", opts.onKeyDown);
  return node;
}
