/* A made video's download menu: the video as made, or converted on the server into another size for
   a site that wants one (POST /chat/video/{message}/export, asked again until it is ready; the sizes
   are app/tools/video_story.py EXPORTS).

   Used by the Videos page and the playground's video card:
   videoDownloads(container, {messageId, lang, url, seconds, label}) fills `container`; label names
   the language when a story has several.
*/
(function(){
  const SIZES = [
    {key: null, name: 'Original · 720p HD', note: 'As made: YouTube, Facebook and any other site'},
    {key: '1080p', name: '1080p Full HD', note: 'Looks sharpest on YouTube and Facebook'},
    {key: 'vertical', name: 'Vertical 9:16 · 1080×1920', note: 'Reels, Shorts, TikTok, Stories', shorts: true},
    {key: 'square', name: 'Square 1:1 · 1080×1080', note: 'A Facebook or Instagram feed post'},
    {key: '480p', name: '480p', note: 'Smaller: WhatsApp, Messenger'},
    {key: '360p', name: '360p', note: 'Smallest, for a slow connection'},
  ];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const clock = s => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;

  const STYLE = `
.vd{position:relative;display:inline-block}
.vd-menu{position:absolute;z-index:20;left:0;margin-top:.3rem;min-width:17rem;background:var(--card);
  border:1px solid var(--border);border-radius:10px;padding:.3rem;box-shadow:0 10px 30px rgba(0,0,0,.35)}
.vd-menu[hidden]{display:none}
.vd-menu button{display:block;width:100%;text-align:left;background:none;border:0;border-radius:7px;
  padding:.45rem .6rem;color:var(--text);cursor:pointer;font:inherit}
.vd-menu button:hover:not(:disabled){background:var(--input)}
.vd-menu button:disabled{opacity:.5;cursor:default}
.vd-menu b{display:block;font-size:.88rem;font-weight:600}
.vd-menu small{display:block;font-size:.76rem;color:var(--muted)}
.vd-menu small.warn{color:var(--warning)}
.vd-state{font-size:.8rem;color:var(--muted);margin-top:.3rem}
.vd-state.bad{color:var(--danger)}`;

  function addStyle(){
    if(document.getElementById('vd-style')) return;
    const style = document.createElement('style');
    style.id = 'vd-style';
    style.textContent = STYLE;
    document.head.appendChild(style);
    // Once for every menu on the page: a click elsewhere, or Escape, closes an open one
    const close = except => document.querySelectorAll('.vd').forEach(vd => {
      if(vd.contains(except)) return;
      vd.querySelector('.vd-menu').hidden = true;
      vd.querySelector('.vd-toggle').setAttribute('aria-expanded', 'false');
    });
    document.addEventListener('click', e => close(e.target));
    document.addEventListener('keydown', e => { if(e.key === 'Escape') close(null); });
  }

  function save(url){
    const link = document.createElement('a');
    link.href = url;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

  window.videoDownloads = function(container, {messageId, lang, url, seconds, label}){
    addStyle();
    container.innerHTML = `<div class="vd">
      <button type="button" class="btn btn-secondary btn-sm btn-small vd-toggle" aria-haspopup="true" aria-expanded="false">⬇ Download${
        label ? ' the ' + esc(label) : ''} video ▾</button>
      <div class="vd-menu" role="menu" hidden>${(messageId ? SIZES : SIZES.slice(0, 1)).map((size, n) => `<button type="button" role="menuitem" data-n="${n}">
        <b>${esc(size.name)}</b><small>${esc(size.note)}</small>${
        size.shorts && seconds > 180 ? `<small class="warn">YouTube Shorts take up to 3:00; this video is ${clock(seconds)}</small>` : ''}
      </button>`).join('')}</div>
      <div class="vd-state" hidden></div></div>`;
    const toggle = container.querySelector('.vd-toggle');
    const menu = container.querySelector('.vd-menu');
    const state = container.querySelector('.vd-state');
    const show = open => {
      menu.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      if(!open) return;
      // Opened under the button, towards the side with room in the window and in the panel it scrolls in
      menu.style.left = '0';
      menu.style.right = 'auto';
      let edge = innerWidth;
      for(let box = container.parentElement; box; box = box.parentElement){
        if(getComputedStyle(box).overflowX !== 'visible'){ edge = Math.min(edge, box.getBoundingClientRect().right); break; }
      }
      if(menu.getBoundingClientRect().right > edge - 8){ menu.style.left = 'auto'; menu.style.right = '0'; }
    };
    toggle.addEventListener('click', () => show(menu.hidden));
    const say = (text, bad) => { state.hidden = !text; state.textContent = text || ''; state.classList.toggle('bad', !!bad); };

    menu.querySelectorAll('button').forEach(item => item.addEventListener('click', async () => {
      show(false);
      const size = SIZES[Number(item.dataset.n)];
      if(!size.key){ save(`${url}?download=1`); return; }
      item.disabled = true;
      try{
        while(true){
          const response = await fetch(`/chat/video/${messageId}/export`, {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({lang, preset: size.key})});
          if(response.status === 401){ location.href = '/login'; return; }
          const body = await response.json().catch(() => ({}));
          if(!response.ok) throw new Error((typeof body.detail === 'string' && body.detail) || `HTTP ${response.status}`);
          if(body.error) throw new Error(body.error);
          if(body.ready){ say(''); save(body.url); return; }
          say(body.waiting ? `${size.name}: waiting for another video to finish converting…`
            : `Preparing ${size.name}… ${Math.round((body.fraction || 0) * 100)}%`);
          await pause(1500);
        }
      }catch(e){
        say(`${size.name} could not be made: ${e.message}`, true);
      }finally{
        item.disabled = false;
      }
    }));
  };
})();
