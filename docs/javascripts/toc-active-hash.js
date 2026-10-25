(function () {
  const activeClass = "md-nav__link--active";
  const passedClass = "md-nav__link--passed";
  let pending = false;

  function linkHash(link) {
    try {
      return new URL(link.getAttribute("href"), window.location.href).hash;
    } catch (error) {
      return "";
    }
  }

  function syncActiveHash() {
    const { hash } = window.location;
    if (!hash) {
      return;
    }

    document.querySelectorAll('.md-sidebar--secondary [data-md-component="toc"]').forEach((toc) => {
      const links = Array.from(toc.querySelectorAll("a.md-nav__link[href]"));
      const active = links.find((link) => linkHash(link) === hash);
      if (!active) {
        return;
      }

      links.forEach((link) => link.classList.remove(activeClass));
      active.classList.remove(passedClass);
      active.classList.add(activeClass);
    });
  }

  function scheduleSync() {
    if (pending) {
      return;
    }

    pending = true;
    window.requestAnimationFrame(() => {
      pending = false;
      syncActiveHash();
      window.setTimeout(syncActiveHash, 75);
      window.setTimeout(syncActiveHash, 250);
    });
  }

  document.addEventListener(
    "click",
    (event) => {
      if (
        event.target instanceof Element &&
        event.target.closest(
          '.md-sidebar--secondary [data-md-component="toc"] a.md-nav__link[href^="#"]',
        )
      ) {
        scheduleSync();
      }
    },
    true,
  );

  window.addEventListener("hashchange", scheduleSync);
  window.addEventListener("popstate", scheduleSync);
  window.addEventListener("scroll", scheduleSync, { passive: true });

  if (window.document$) {
    window.document$.subscribe(scheduleSync);
  } else {
    document.addEventListener("DOMContentLoaded", scheduleSync);
  }
})();
