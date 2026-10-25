// Passive, deliberately small Markdown renderer for creator-authored notes.
// It supports headings, lists, quotes, code fences, emphasis, and safe HTTPS
// links. Raw HTML is always escaped, and images are rendered as text; package
// content must never become an executable browser surface.
export function sanitizeMarkdown(value) {
  return [...String(value == null ? "" : value)]
    .filter((character) => {
      const code = character.codePointAt(0);
      return !(
        (code >= 0 && code <= 31 && ![9, 10, 13].includes(code)) ||
        (code >= 127 && code <= 159) ||
        (code >= 0x202a && code <= 0x202e) ||
        (code >= 0x2066 && code <= 0x2069)
      );
    })
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : "";
  } catch (_) {
    return "";
  }
}

function applyEmphasis(escaped) {
  return (
    escaped
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
      // Underscore emphasis must not consume a fragment of snake_case text.
      .replace(/(^|[^A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])/g, "$1<em>$2</em>")
  );
}

function renderTextSegment(source) {
  return applyEmphasis(escapeHtml(source));
}

function renderInline(source) {
  const tokenPattern = /!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\(([^)\s]+)\)/y;
  let html = "";
  let buffer = "";
  let cursor = 0;
  const flush = () => {
    if (buffer) html += renderTextSegment(buffer);
    buffer = "";
  };
  while (cursor < source.length) {
    tokenPattern.lastIndex = cursor;
    const match = tokenPattern.exec(source);
    if (!match) {
      buffer += source[cursor];
      cursor += 1;
      continue;
    }
    flush();
    const isImage = match[1] !== undefined;
    const label = isImage ? match[1] : match[3];
    const href = safeUrl(isImage ? match[2] : match[4]);
    // Images are intentionally reduced to escaped alt text. Never emit an
    // image URL or a synthetic link for untrusted package content.
    if (isImage || !href) html += renderTextSegment(label);
    else
      html += `<a href="${escapeHtml(href)}" rel="noopener noreferrer">${renderTextSegment(label)}</a>`;
    cursor = tokenPattern.lastIndex;
  }
  flush();
  return html;
}

export function renderMarkdown(value) {
  const lines = sanitizeMarkdown(value).split(/\r?\n/);
  const output = [];
  let paragraph = [];
  let list = null;
  let code = null;
  const flushParagraph = () => {
    if (paragraph.length) output.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    output.push(
      `<${list.kind}>${list.items.map((item) => `<li>${renderInline(item)}</li>`).join("")}</${list.kind}>`,
    );
    list = null;
  };
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      flushParagraph();
      flushList();
      if (code === null) code = [];
      else {
        output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = null;
      }
      continue;
    }
    if (code !== null) {
      code.push(line);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,4})\s+(.+?)\s*#*$/);
    if (heading) {
      flushParagraph();
      flushList();
      output.push(
        `<h${Math.min(heading[1].length + 1, 5)}>${renderInline(heading[2])}</h${Math.min(heading[1].length + 1, 5)}>`,
      );
      continue;
    }
    const item = line.match(/^\s*([-*+] |\d+[.)] )(.+)$/);
    if (item) {
      flushParagraph();
      const kind = /^\d/.test(item[1]) ? "ol" : "ul";
      if (!list || list.kind !== kind) {
        flushList();
        list = { kind, items: [] };
      }
      list.items.push(item[2]);
      continue;
    }
    if (/^\s*> ?/.test(line)) {
      flushParagraph();
      flushList();
      output.push(`<blockquote>${renderInline(line.replace(/^\s*> ?/, ""))}</blockquote>`);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    paragraph.push(line.trim());
  }
  if (code !== null) output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  flushParagraph();
  flushList();
  return output.join("\n");
}
