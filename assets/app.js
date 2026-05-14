
/* Pajero IV Manual */
(function () {
  'use strict';

  const base = () => document.querySelector('meta[name=base-url]')?.content || '/';

  // ── Image lightbox ──
  const lb = document.getElementById('lb');
  if (lb) {
    const lbImg = lb.querySelector('img');
    document.querySelectorAll('.manual-content img').forEach(img => {
      img.addEventListener('click', () => { lbImg.src = img.src; lb.classList.add('open'); });
    });
    lb.addEventListener('click', () => lb.classList.remove('open'));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') lb.classList.remove('open'); });
  }

  // ── Header search ──
  const sf = document.querySelector('.search-form');
  if (sf) {
    sf.addEventListener('submit', e => {
      e.preventDefault();
      const q = sf.querySelector('input').value.trim();
      if (q) window.location.href = base() + 'search.html?q=' + encodeURIComponent(q);
    });
  }

  // ── Mobile sidebar ──
  const toggle = document.getElementById('sb-toggle');
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sb-overlay');
  if (toggle && sidebar) {
    const open  = () => { sidebar.classList.add('open');    overlay?.classList.add('open'); };
    const close = () => { sidebar.classList.remove('open'); overlay?.classList.remove('open'); };
    toggle.addEventListener('click', () => sidebar.classList.contains('open') ? close() : open());
    overlay?.addEventListener('click', close);
    // close on nav (mobile)
    sidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', close));
  }

  // ── Persistent nav state ──
  const metaYear   = document.querySelector('meta[name=view-year]');
  const metaManual = document.querySelector('meta[name=view-manual]');
  if (metaYear && metaManual) {
    try {
      localStorage.setItem('pajero_year', metaYear.content);
      localStorage.setItem('pajero_manual', metaManual.content);
    } catch(e) {}
  }
  // Resume banner on landing
  const banner = document.getElementById('resume-banner');
  if (banner) {
    try {
      const y = localStorage.getItem('pajero_year');
      const m = localStorage.getItem('pajero_manual');
      if (y && m) {
        const yLabels = JSON.parse(document.getElementById('year-labels-json')?.textContent || '{}');
        const mNames  = JSON.parse(document.getElementById('manual-names-json')?.textContent || '{}');
        const label = (mNames[m] || m) + ' · ' + (yLabels[y] || y);
        const link = banner.querySelector('a');
        link.textContent = label;
        link.href = base() + 'view/' + y + '/' + m + '/index.html';
        banner.style.display = 'flex';
        banner.querySelector('.rb-dismiss').addEventListener('click', () => {
          banner.style.display = 'none';
          try { localStorage.removeItem('pajero_year'); localStorage.removeItem('pajero_manual'); } catch(e) {}
        });
      }
    } catch(e) {}
  }

  // ── Page TOC ──
  const content = document.querySelector('.manual-content');
  if (content) {
    const headings = [...content.querySelectorAll('h2, h3')];
    if (headings.length >= 3) {
      headings.forEach((h, i) => { if (!h.id) h.id = 'toc-' + i; });
      const items = headings.map(h => {
        const cls = h.tagName === 'H3' ? 'toc-h3' : '';
        return `<li class="${cls}"><a href="#${h.id}">${h.textContent.trim()}</a></li>`;
      }).join('');
      const toc = document.createElement('details');
      toc.className = 'page-toc';
      if (window.innerWidth >= 900) toc.open = true;
      toc.innerHTML = `<summary><span class="toc-arrow">▶</span>&nbsp;On this page (${headings.length})</summary><ul class="toc-list">${items}</ul>`;
      content.parentNode.insertBefore(toc, content);
    }
  }

  // ── Sidebar sub-pages ──
  const siblingsEl = document.getElementById('pg-siblings');
  if (siblingsEl) {
    try {
      const data = JSON.parse(siblingsEl.textContent);
      const activeLink = document.querySelector('.sidebar a.active');
      if (activeLink && data.pages.length > 1) {
        const sub = document.createElement('div');
        sub.className = 'sb-subpages';
        sub.innerHTML = data.pages.map(p => {
          const cls = p.id === data.current ? 'current' : '';
          const title = p.title.length > 42 ? p.title.slice(0, 42) + '…' : p.title;
          return `<a class="${cls}" href="${base()}page/${p.id}.html" title="${p.title}">${title}</a>`;
        }).join('');
        activeLink.insertAdjacentElement('afterend', sub);
        sub.querySelector('.current')?.scrollIntoView({ block: 'nearest' });
      }
    } catch(e) {}
  }
})();

/* ── Landing page search ── */
(function() {
  const form = document.getElementById('landing-search');
  if (!form) return;
  form.addEventListener('submit', e => {
    e.preventDefault();
    const q = form.querySelector('input').value.trim();
    if (q) {
      const base = document.querySelector('meta[name=base-url]')?.content || '/';
      window.location.href = base + 'search.html?q=' + encodeURIComponent(q);
    }
  });
})();

/* ── Feedback button ── */
window.openFeedback = function() {
  const title  = encodeURIComponent('Feedback: ' + document.title);
  const body   = encodeURIComponent('Page: ' + window.location.href + '\n\nFeedback:\n\n');
  const email  = document.querySelector('meta[name=feedback-email]')?.content || '';
  if (email) {
    window.location.href = 'mailto:' + email + '?subject=' + title + '&body=' + body;
  } else {
    alert('Feedback email not configured.');
  }
};

