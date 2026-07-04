(function() {
  // ── 메인 탭 전환 (현재 경량 / 레거시 분산) ──
  document.querySelectorAll('.main-tab').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.querySelectorAll('.main-tab').forEach(function(b) { b.classList.remove('main-tab-active'); });
      document.getElementById('main-panel-current').classList.add('main-panel-hidden');
      document.getElementById('main-panel-legacy').classList.add('main-panel-hidden');
      
      btn.classList.add('main-tab-active');
      var targetId = 'main-panel-' + btn.dataset.mainTab;
      var target = document.getElementById(targetId);
      if (target) target.classList.remove('main-panel-hidden');
    });
  });

  // ── 파이프라인 내부 탭 전환 ──
  document.querySelectorAll('.pipe-tabs .pipe-tab').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      
      var parentPanel = btn.closest('.main-panel');
      parentPanel.querySelectorAll('.pipe-tab').forEach(function(b) { b.classList.remove('active'); });
      parentPanel.querySelectorAll('.pipe-panel').forEach(function(p) { p.classList.add('hidden'); });
      
      btn.classList.add('active');
      var tabId = 'tab-' + btn.dataset.tab;
      var target = parentPanel.querySelector('#' + tabId);
      if (target) target.classList.remove('hidden');
    }, true);
  });

  /* particles */
  const wrap = document.getElementById('lkParticles');
  if (wrap) {
    const colors = ['#d4522a', '#2563a8', '#2d8a5e', '#b07d2c', '#e8824a'];
    for (let i = 0; i < 18; i++) {
        const el = document.createElement('div');
        el.className = 'lk-particle';
        const s = Math.random() * 8 + 3;
        el.style.cssText = `width:${s}px;height:${s}px;left:${Math.random() * 100}%;background:${colors[i % colors.length]};--d:${Math.random() * 13 + 9}s;--dl:${Math.random() * 9}s;`;
        wrap.appendChild(el);
    }
  }

  /* scroll reveal */
  const io = new IntersectionObserver(en => { en.forEach(e => { if (e.isIntersecting) e.target.classList.add('on'); }); }, { threshold: .08 });
  document.querySelectorAll('.rv,.rvl,.rvr').forEach(el => io.observe(el));

  /* stat counters animation */
  const so = new IntersectionObserver(en => {
      en.forEach(e => {
          if (e.isIntersecting) {
              e.target.querySelectorAll('.stat-num').forEach((n, i) => {
                  setTimeout(() => {
                      n.style.transition = 'transform .38s cubic-bezier(.34,1.56,.64,1)';
                      n.style.transform = 'scale(1.16)';
                      setTimeout(() => { n.style.transform = 'scale(1)'; }, 380);
                  }, i * 90);
              });
              so.unobserve(e.target);
          }
      });
  }, { threshold: .5 });
  document.querySelectorAll('.stat-strip').forEach(el => so.observe(el));
})();
