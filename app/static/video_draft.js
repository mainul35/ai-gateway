/* A video story's scenes before its video is made: text only, to read and correct.

   Used by the Videos page. Nothing here draws or voices anything; "Make the video" is the one
   button that spends the GPUs' time (POST /chat/video/{message}/produce). The scenes as corrected are
   kept with PUT /chat/video/{message}/draft, and words missing in a language - a part written in one
   language only - are translated by POST /chat/video/{message}/prepare.

   renderDraft(container, current, hooks): current is GET /chat/videos/{id}; hooks are
   {showError(text), readEvents(response, onData), progress(event | null), reload()}.
*/
(function(){
  const NAMES = {en: 'English', bn: 'বাংলা', es: 'Español', fr: 'Français', zh: '中文', ja: '日本語', ko: '한국어',
                 th: 'ไทย', ar: 'العربية', ur: 'اردو', hi: 'हिन्दी'};
  // The kinds of speaker and how many voices each has (app/tools/video_story.py KINDS)
  const KINDS = {narrator: 1, man: 3, 'older man': 2, woman: 3, 'older woman': 2, boy: 2, girl: 2};
  const KIND_LABEL = {narrator: 'Narrator', man: 'Man', 'older man': 'Older man', woman: 'Woman',
                      'older woman': 'Older woman', boy: 'Boy', girl: 'Girl'};
  const JSON_HEADERS = {'Content-Type': 'application/json'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const clean = text => (text || '').split(/\s+/).join(' ').trim();

  const STYLE = `
.draft-head{display:flex;flex-wrap:wrap;gap:.7rem;align-items:center;margin-bottom:1rem}
.draft-head h2{margin:0;font-size:1.3rem;color:var(--text)}
.draft-head .spacer{flex:1}
.draft-note{margin:.2rem 0 1rem;color:var(--muted);font-size:.9rem}
.draft-cast{background:var(--card);border-radius:12px;padding:.8rem 1rem;margin-bottom:1rem}
.draft-cast summary{cursor:pointer;font-weight:600}
.cast-row{display:grid;grid-template-columns:minmax(8rem,14rem) 10rem 8rem auto;gap:.5rem;align-items:center;margin-top:.5rem}
@media (max-width:640px){.cast-row{grid-template-columns:1fr 1fr}}
.cast-row input,.cast-row select{width:100%}
.cast-voice{margin:0 0 .5rem 1.5rem;font-size:.72rem;opacity:.75}
.draft-scenes{display:flex;flex-direction:column;gap:.9rem}
.draft-scene{background:var(--card);border-radius:12px;padding:.8rem 1rem;border:1px solid transparent}
.draft-scene.changed{border-color:#f59e0b}
.draft-scene .bar{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin-bottom:.5rem}
.draft-scene .bar b{margin-right:auto}
.draft button.small{background:none;border:1px solid var(--border);border-radius:7px;color:var(--text);
  padding:.15rem .55rem;cursor:pointer;font-size:.8rem;justify-self:start;width:auto}
.draft button.small:disabled{opacity:.4;cursor:default}
.draft button.danger{border-color:#7f1d1d;color:#fca5a5}
.draft-part{display:grid;grid-template-columns:10rem repeat(var(--languages,2),minmax(0,1fr)) auto;gap:.5rem;
  align-items:start;padding:.45rem 0;border-top:1px dashed var(--border)}
@media (max-width:900px){.draft-part{grid-template-columns:1fr}}
.draft-part select{width:100%}
.draft-part textarea{width:100%;min-height:4.2rem;resize:vertical;font-family:inherit;font-size:.93rem;line-height:1.5}
.draft-part .lang{font-size:.72rem;color:var(--muted);margin-bottom:.15rem}
.draft-part textarea.missing{border-color:#f59e0b}
.draft-picture{display:grid;grid-template-columns:1fr 12rem;gap:.5rem;margin-top:.5rem}
@media (max-width:640px){.draft-picture{grid-template-columns:1fr}}
.draft-picture textarea{width:100%;min-height:3rem;resize:vertical;font-family:inherit;font-size:.85rem}
.draft-picture .lang{font-size:.72rem;color:var(--muted);margin-bottom:.15rem}
.draft-sacred{font-size:.8rem;color:var(--muted);margin-top:.4rem}`;

  function addStyle(){
    if(document.getElementById('draftStyle')) return;
    const style = document.createElement('style');
    style.id = 'draftStyle';
    style.textContent = STYLE;
    document.head.appendChild(style);
  }

  function voiceSlots(kind){
    if(kind === 'narrator') return ['narrator'];
    return Array.from({length: KINDS[kind] || 1}, (_, n) => `${kind.replace(' ', '-')}-${n + 1}`);
  }

  // The voice a character is given, in the story's languages: a voice the model has, or the
  // language's one voice, turned (app/tools/voices.py, sent as voice_plan)
  function voiceNote(plan, member, langs){
    return langs.map(l => {
      const info = (plan[l] || {})[member.voice];
      return info ? `${(NAMES[l] || l)}: ${info.turned ? 'turned' : info.name}` : '';
    }).filter(Boolean).join('  ·  ');
  }

  window.renderDraft = function(container, current, hooks){
    addStyle();
    const video = current.video;
    const told = video.story || {};
    const langs = Object.keys(NAMES).filter(l => told[l] || (video.languages || []).includes(l));
    // The working copy: changed here, kept by Save
    const draft = {
      cast: JSON.parse(JSON.stringify(told.cast || [{id: 'narrator', name: 'Narrator', kind: 'narrator', voice: 'narrator'}])),
      scenes: (told.parts || (told[langs[0]] || []).map((_, i) => [{speaker: 'narrator',
        text: Object.fromEntries(langs.map(l => [l, (told[l] || [])[i] || '']))}])).map((parts, i) => ({
          parts: JSON.parse(JSON.stringify(parts)), picture: (told.drawn || [])[i] || '',
          people: (told.people || [])[i] || 'from_behind',
          notes: ((told.notes || [])[i] || []).join('
')}))
    };
    let dirty = false, working = false;

    function markDirty(){
      dirty = true;
      container.querySelector('#draftSave').disabled = working;
      container.querySelector('#draftState').textContent = 'Changes not saved yet';
    }

    function speakerOptions(selected){
      return draft.cast.map(m => `<option value="${esc(m.id)}" ${m.id === selected ? 'selected' : ''}>${
        esc(m.name || KIND_LABEL[m.kind])}${m.kind !== 'narrator' ? ' · ' + KIND_LABEL[m.kind] : ''}</option>`).join('');
    }

    function renderCast(){
      const box = container.querySelector('#draftCast');
      box.innerHTML = `<summary>Who speaks · ${draft.cast.length - 1} character${draft.cast.length === 2 ? '' : 's'} and the narrator</summary>
        ${draft.cast.map((m, k) => `<div class="cast-row" data-k="${k}">
          <input value="${esc(m.name)}" ${m.id === 'narrator' ? 'disabled' : ''} data-field="name" placeholder="Name">
          <select data-field="kind" ${m.id === 'narrator' ? 'disabled' : ''}>${Object.keys(KINDS).filter(kind =>
            m.id === 'narrator' ? kind === 'narrator' : kind !== 'narrator').map(kind =>
            `<option value="${kind}" ${kind === m.kind ? 'selected' : ''}>${KIND_LABEL[kind]}</option>`).join('')}</select>
          <select data-field="voice">${voiceSlots(m.kind).map((slot, n) =>
            `<option value="${slot}" ${slot === m.voice ? 'selected' : ''}>${m.kind === 'narrator' ? 'Narrator voice' : 'Voice ' + (n + 1)}</option>`).join('')}</select>
          ${m.id === 'narrator' ? '<span></span>' : `<button type="button" class="small danger" data-remove-cast="${k}"
            title="Take this character out; their parts become the narrator's">✕</button>`}</div>
          ${(() => { const note = voiceNote(current.voice_plan || {}, m, langs); return note ? `<div class="cast-voice">${esc(note)}</div>` : ''; })()}`).join('')}
        <div style="margin-top:.6rem"><button type="button" class="small" id="addCast">＋ Add a character</button></div>
        <div class="draft-sacred">The words of Allah, of a prophet or of an angel are always read in the narrator's
          voice, never given to a character.</div>`;
      box.querySelectorAll('.cast-row').forEach(row => row.addEventListener('change', e => {
        const member = draft.cast[Number(row.dataset.k)];
        member[e.target.dataset.field] = e.target.value;
        if(e.target.dataset.field === 'kind') member.voice = voiceSlots(member.kind)[0];
        markDirty();
        renderCast();
        renderScenes();
      }));
      box.querySelectorAll('[data-remove-cast]').forEach(b => b.addEventListener('click', () => {
        const [gone] = draft.cast.splice(Number(b.dataset.removeCast), 1);
        draft.scenes.forEach(s => s.parts.forEach(p => { if(p.speaker === gone.id) p.speaker = 'narrator'; }));
        markDirty(); renderCast(); renderScenes();
      }));
      box.querySelector('#addCast').addEventListener('click', () => {
        let n = 1;
        while(draft.cast.some(m => m.id === 'c' + n)) n++;
        draft.cast.push({id: 'c' + n, name: '', kind: 'man', voice: voiceSlots('man')[0]});
        markDirty(); renderCast(); renderScenes();
        box.open = true;
      });
    }

    function renderScenes(){
      const list = container.querySelector('#draftScenes');
      list.style.setProperty('--languages', langs.length);
      list.innerHTML = draft.scenes.map((scene, i) => `<div class="draft-scene" data-i="${i}">
        <div class="bar"><b>Scene ${i + 1}</b>
          <button type="button" class="small" data-act="up" ${i === 0 ? 'disabled' : ''} title="Move up">↑</button>
          <button type="button" class="small" data-act="down" ${i === draft.scenes.length - 1 ? 'disabled' : ''} title="Move down">↓</button>
          <button type="button" class="small" data-act="add" title="Add an empty scene after this one">＋ Scene after</button>
          <button type="button" class="small danger" data-act="delete" ${draft.scenes.length === 1 ? 'disabled' : ''}>Delete scene</button></div>
        ${scene.parts.map((part, k) => `<div class="draft-part" data-k="${k}">
          <div><div class="lang">Speaker</div><select data-field="speaker">${speakerOptions(part.speaker)}</select></div>
          ${langs.map(l => `<div><div class="lang">${NAMES[l]}</div>
            <textarea data-lang="${l}" lang="${l}" dir="auto" class="${clean(part.text?.[l]) ? '' : 'missing'}"
              placeholder="${l === langs[0] ? 'What is said' : 'Left empty: translated when you fill in translations'}">${esc(part.text?.[l] || '')}</textarea></div>`).join('')}
          <div style="display:flex;flex-direction:column;gap:.3rem;padding-top:1rem">
            <button type="button" class="small" data-act="part-after" title="Add a part after this one">＋ Part</button>
            <button type="button" class="small danger" data-act="part-remove" ${scene.parts.length === 1 ? 'disabled' : ''} title="Remove this part">✕</button></div>
        </div>`).join('')}
        <div class="draft-picture"><div><div class="lang">The picture: what it should show</div>
            <textarea data-field="picture">${esc(scene.picture)}</textarea></div>
          <div><div class="lang">People in it</div><select data-field="people">
            <option value="from_behind" ${scene.people === 'from_behind' ? 'selected' : ''}>People, seen from behind</option>
            <option value="none" ${scene.people === 'none' ? 'selected' : ''}>No people</option></select></div></div>
        <div class="draft-picture"><div><div class="lang">Notes after this scene: shown on a dark screen, not read
            (references, footnotes; one a line)</div>
            <textarea data-field="notes" dir="auto" placeholder="None">${esc(scene.notes || '')}</textarea></div></div>
      </div>`).join('');
      list.querySelectorAll('.draft-scene').forEach(card => {
        const i = Number(card.dataset.i);
        card.addEventListener('input', e => {
          const scene = draft.scenes[i];
          const part = e.target.closest('.draft-part');
          if(part && e.target.dataset.lang){
            scene.parts[Number(part.dataset.k)].text = {...scene.parts[Number(part.dataset.k)].text, [e.target.dataset.lang]: e.target.value};
            e.target.classList.toggle('missing', !clean(e.target.value));
          }else if(e.target.dataset.field === 'picture'){
            scene.picture = e.target.value;
          }else if(e.target.dataset.field === 'notes'){
            scene.notes = e.target.value;
          }
          card.classList.add('changed');
          markDirty();
        });
        card.addEventListener('change', e => {
          const scene = draft.scenes[i];
          const part = e.target.closest('.draft-part');
          if(e.target.dataset.field === 'speaker') scene.parts[Number(part.dataset.k)].speaker = e.target.value;
          if(e.target.dataset.field === 'people') scene.people = e.target.value;
          card.classList.add('changed');
          markDirty();
        });
        card.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => {
          const act = b.dataset.act;
          const k = Number(b.closest('.draft-part')?.dataset.k);
          if(act === 'up') [draft.scenes[i - 1], draft.scenes[i]] = [draft.scenes[i], draft.scenes[i - 1]];
          if(act === 'down') [draft.scenes[i + 1], draft.scenes[i]] = [draft.scenes[i], draft.scenes[i + 1]];
          if(act === 'add') draft.scenes.splice(i + 1, 0, {parts: [{speaker: 'narrator', text: {}}], picture: '', people: 'from_behind', notes: ''});
          if(act === 'delete'){
            if(!confirm(`Delete scene ${i + 1}?`)) return;
            draft.scenes.splice(i, 1);
          }
          if(act === 'part-after') draft.scenes[i].parts.splice(k + 1, 0, {speaker: 'narrator', text: {}});
          if(act === 'part-remove') draft.scenes[i].parts.splice(k, 1);
          markDirty();
          renderScenes();
        }));
      });
      container.querySelector('#draftCount').textContent = `${draft.scenes.length} scene${draft.scenes.length === 1 ? '' : 's'}`;
    }

    async function save(){
      const scenes = draft.scenes.map(s => ({
        parts: s.parts.map(p => ({speaker: p.speaker, text: Object.fromEntries(Object.entries(p.text || {})
          .map(([l, t]) => [l, clean(t)]).filter(([, t]) => t))})), picture: clean(s.picture), people: s.people,
        notes: (s.notes || '').split('
').map(clean).filter(Boolean)}));
      if(scenes.some(s => s.parts.every(p => !Object.keys(p.text).length))){
        throw new Error('Every scene needs some words; delete an empty one');
      }
      const response = await fetch(`/chat/video/${current.message_id}/draft`, {method: 'PUT', headers: JSON_HEADERS,
        body: JSON.stringify({scenes, cast: draft.cast})});
      const body = await response.json().catch(() => ({}));
      if(!response.ok) throw new Error((typeof body.detail === 'string' && body.detail) || `HTTP ${response.status}`);
      dirty = false;
      return body;
    }

    // A job on the draft - filling in translations, or making the video - followed to its end
    async function run(path){
      working = true;
      container.querySelectorAll('button, textarea, input, select').forEach(el => el.disabled = true);
      hooks.progress({stage: 'Starting', fraction: 0});
      try{
        const response = await fetch(path, {method: 'POST'});
        if(!response.ok){
          const err = await response.json().catch(() => ({}));
          throw new Error((typeof err.detail === 'string' && err.detail) || `HTTP ${response.status}`);
        }
        await hooks.readEvents(response, chunk => { if(chunk.gateway?.type === 'progress') hooks.progress(chunk.gateway); });
      }catch(e){
        hooks.showError('It stopped: ' + e.message + '. What was done is kept.');
      }
      working = false;
      hooks.progress(null);
      hooks.reload();
    }

    container.classList.add('draft');
    container.innerHTML = `<div class="draft-head"><h2>${esc(video.title || 'Video story')}</h2>
        <span class="muted" id="draftCount"></span><span class="muted">· ${langs.map(l => NAMES[l]).join(' · ')}</span>
        <span class="spacer"></span><span class="muted" id="draftState">Saved</span>
        <button type="button" class="btn btn-secondary" id="draftSave" disabled>Save</button>
        <button type="button" class="btn btn-secondary" id="draftFill" title="Translate the words missing in any language">Fill in translations</button>
        <button type="button" class="btn" id="draftMake">Make the video ›</button></div>
      <div class="draft-note">Nothing has been drawn or voiced yet. Read the scenes, correct the words and who says them,
        delete or add scenes, say what each picture should show - then make the video.
        ${video.paused ? `<br><span style="color:#f59e0b">${esc(video.paused)}</span>` : ''}</div>
      <details class="draft-cast" id="draftCast"></details>
      <div class="draft-scenes" id="draftScenes"></div>`;
    renderCast();
    renderScenes();
    if(current.being_made){
      container.querySelector('.draft-note').insertAdjacentHTML('beforeend',
        '<br><b>Being prepared right now; reload in a moment.</b>');
      container.querySelectorAll('button, textarea, input, select').forEach(el => el.disabled = true);
    }

    container.querySelector('#draftSave').addEventListener('click', async () => {
      try{
        const result = await save();
        container.querySelector('#draftState').textContent = result.needs_translation
          ? 'Saved · some words still to be translated' : 'Saved';
        container.querySelector('#draftSave').disabled = true;
        hooks.showError('');
      }catch(e){ hooks.showError(e.message); }
    });
    container.querySelector('#draftFill').addEventListener('click', async () => {
      try{ if(dirty) await save(); }catch(e){ hooks.showError(e.message); return; }
      run(`/chat/video/${current.message_id}/prepare`);
    });
    container.querySelector('#draftMake').addEventListener('click', async () => {
      if(!confirm(`Make the video now? Its ${draft.scenes.length} pictures are drawn and every part voiced, in ` +
                  `${langs.length} language${langs.length === 1 ? '' : 's'} - this takes a while.`)) return;
      try{ if(dirty) await save(); }catch(e){ hooks.showError(e.message); return; }
      run(`/chat/video/${current.message_id}/produce`);
    });
    window.addEventListener('beforeunload', e => { if(dirty) e.preventDefault(); });
  };
})();
