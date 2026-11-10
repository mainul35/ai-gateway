/* The formatting strip of the document editor, after MDViewer Cloud's (web/components/EditorToolbar.tsx):
   letterforms for bold, italic and strikethrough, drawn shapes for the rest, so the marks take the
   theme's colour. It edits the Markdown, and is plain about it: bold is two asterisks appearing.

   It acts on what is selected in either pane. Words selected in the preview are found in the text
   by how many times they appear before the selection - the third "the" stays the third - and the
   editor's own selection, or its caret, is used when nothing is selected there.

   Every change goes in as typing would (execCommand insertText), so Ctrl+Z takes it back.

   Also here: drawing ```chart blocks with mdchart (static/mdchart), for the preview and the PDF. */
(function(){
  'use strict';

  // The forms, with what each is for - the same list and words as MDViewer's picker
  const FORMS = [
    ['bar', 'Bar', 'Compare named things - long labels stay readable'],
    ['column', 'Column', 'Compare across a few steps or dates'],
    ['line', 'Line', 'Change over time'],
    ['area', 'Area', 'Change over time, with the total emphasised'],
    ['pie', 'Pie', 'Parts of a whole, up to six'],
    ['donut', 'Donut', 'Parts of a whole, with room for a total'],
    ['stat', 'Stat', 'One number that matters, with its change'],
  ];

  // Example data that suits the form, so a new chart draws something the moment it appears
  function chartBody(type){
    switch(type){
      case 'column': return 'x: Mon, Tue, Wed, Thu\n---\nFirst  | 120, 140, 131, 128\nSecond | 340, 352, 377, 361';
      case 'line': case 'area': return 'x: Jan, Feb, Mar, Apr, May\n---\nValue | 120, 180, 165, 240, 232';
      case 'pie': case 'donut': return 'First  | 60\nSecond | 30\nThird  | 10';
      case 'stat': return 'unit: files\ndelta: +12% vs last week\n---\nTotal | 1284';
      default: return 'First  | 412\nSecond | 208\nThird  |  96';
    }
  }

  const S = 'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"';
  const svg = inner => `<svg viewBox="0 0 16 14" width="16" height="14" aria-hidden="true">${inner}</svg>`;
  const ICONS = {
    code: svg(`<path d="M6 2 L2 7 L6 12" ${S}/><path d="M10 2 L14 7 L10 12" ${S}/>`),
    block: svg(`<rect x="1" y="2" width="14" height="10" rx="2" ${S}/><path d="M6 5.5 L4 7 L6 8.5" ${S}/><path d="M10 5.5 L12 7 L10 8.5" ${S}/>`),
    bullets: svg(`<circle cx="2.5" cy="3.5" r="1.2" fill="currentColor"/><circle cx="2.5" cy="7" r="1.2" fill="currentColor"/>
                  <circle cx="2.5" cy="10.5" r="1.2" fill="currentColor"/><path d="M6 3.5 H14 M6 7 H14 M6 10.5 H14" ${S}/>`),
    numbers: svg(`<text x="0" y="5" font-size="5" fill="currentColor">1</text><text x="0" y="12" font-size="5" fill="currentColor">2</text>
                  <path d="M6 3.5 H14 M6 10.5 H14" ${S}/>`),
    quote: svg(`<path d="M2 2 V12" ${S} stroke-width="2"/><path d="M6 4.5 H14 M6 9.5 H12" ${S}/>`),
    link: svg(`<path d="M6.5 9.5 L9.5 4.5" ${S}/><path d="M6 4 H4 A3 3 0 0 0 4 10 H6" ${S}/><path d="M10 4 H12 A3 3 0 0 1 12 10 H10" ${S}/>`),
    table: svg(`<rect x="1" y="2" width="14" height="10" rx="1.5" ${S}/><path d="M1 5.5 H15 M6 2 V12 M10.5 2 V12" ${S}/>`),
    chart: svg(`<path d="M1 13 H15" ${S}/><rect x="2.5" y="7" width="2.6" height="5" fill="currentColor"/>
                <rect x="6.7" y="3.5" width="2.6" height="8.5" fill="currentColor"/><rect x="10.9" y="5.5" width="2.6" height="6.5" fill="currentColor"/>`),
    image: svg(`<rect x="1" y="2" width="14" height="10" rx="1.5" ${S}/><circle cx="5" cy="5.5" r="1.2" ${S}/>
                <path d="M2 11 L6.5 7 L9 9.5 L11.5 7.5 L14 10.5" ${S}/>`),
    left: svg(`<path d="M1 3 H15" ${S}/><path d="M1 7 H10 M1 11 H12" ${S}/>`),
    center: svg(`<path d="M1 3 H15" ${S}/><path d="M3.5 7 H12.5 M2 11 H14" ${S}/>`),
    right: svg(`<path d="M1 3 H15" ${S}/><path d="M6 7 H15 M4 11 H15" ${S}/>`),
  };

  const lineStart = (s, at) => s.lastIndexOf('\n', Math.max(0, at - 1)) + 1;
  const lineEnd = (s, at) => { const end = s.indexOf('\n', at); return end === -1 ? s.length : end; };

  /* Where words selected in the preview are in the Markdown: the n-th time they appear in the
     rendered text before the selection is the n-th time in the source. Null when it cannot be
     placed - words selected across formatting read differently on screen than in the source. */
  function previewRange(preview, source){
    const selection = window.getSelection();
    if(!selection || !selection.rangeCount || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    if(!preview.contains(range.startContainer)) return null;
    const text = selection.toString();
    if(!text.trim()) return null;
    const before = range.cloneRange();
    before.selectNodeContents(preview);
    before.setEnd(range.startContainer, range.startOffset);
    const prefix = before.toString();
    let ordinal = 0;
    for(let at = prefix.indexOf(text); at >= 0; at = prefix.indexOf(text, at + text.length)) ordinal++;
    let at = -text.length;
    for(let i = 0; i <= ordinal; i++){
      at = source.indexOf(text, at + text.length);
      if(at < 0) return null;
    }
    return {start: at, end: at + text.length};
  }

  /* A chart's drawing from its fence's body; mdchart escapes what it writes */
  function chartHtml(source, width){
    if(!window.MdChart) return null;
    try{ return window.MdChart.render(source, width); }catch(e){ return null; }
  }

  /* Each ```chart block in rendered Markdown, drawn in its place */
  function drawCharts(root){
    if(!window.MdChart) return;
    const width = Math.max(320, (root.clientWidth || 680) - 40);
    for(const code of root.querySelectorAll('pre > code.language-chart')){
      const html = chartHtml(code.textContent, width);
      if(!html) continue;
      const host = document.createElement('div');
      host.className = 'mdc-host mdc-dark';   // the playground is dark; the PDF keeps the light colours
      host.innerHTML = html;
      code.parentElement.replaceWith(host);
    }
  }

  /* The drawings of a document's ```chart blocks in the order they come, for the PDF */
  function chartsOf(text){
    const out = [];
    const fence = /^(```|~~~)chart\b[^\n]*\n([\s\S]*?)^\1[ \t]*$/gm;
    for(let found; (found = fence.exec(text));) out.push(chartHtml(found[2], 680) || '');
    return out;
  }

  /* The strip itself, above `area`. `upload(file)` stores a picture and gives back {url, name}. */
  function attach(bar, area, preview, upload){
    const tools = [
      ['bold', 'Bold (Ctrl+B)', '<b>B</b>', () => wrap('**')],
      ['italic', 'Italic (Ctrl+I)', '<i>I</i>', () => wrap('*')],
      ['strike', 'Strikethrough', '<s>S</s>', () => wrap('~~')],
      ['code', 'Inline code', ICONS.code, () => wrap('`')],
      ['block', 'Code block', ICONS.block, () => insert('```\n\n```', 4)],
      '|',
      ['h1', 'Heading 1', 'H1', () => heading(1)],
      ['h2', 'Heading 2', 'H2', () => heading(2)],
      ['h3', 'Heading 3', 'H3', () => heading(3)],
      '|',
      ['bullets', 'Bullet list', ICONS.bullets, () => prefix('- ')],
      ['numbers', 'Numbered list', ICONS.numbers, () => prefix('', true)],
      ['quote', 'Block quote', ICONS.quote, () => prefix('> ')],
      ['link', 'Link (Ctrl+K)', ICONS.link, () => link()],
      ['table', 'Table', ICONS.table, () => insert('| Heading | Heading |\n| --- | --- |\n|  |  |\n', 2)],
      '|',
      ['chart', 'Insert chart', ICONS.chart, button => pickChart(button)],
      ['image', 'Insert picture', ICONS.image, () => picker.click()],
      ['left', 'Align left', ICONS.left, () => align('left')],
      ['center', 'Centre', ICONS.center, () => align('center')],
      ['right', 'Align right', ICONS.right, () => align('right')],
    ];
    bar.className = 'format-bar';
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', 'Formatting');
    bar.innerHTML = tools.map(t => t === '|' ? '<span class="format-divider"></span>'
      : `<button type="button" class="tool tool-${t[0]}" data-tool="${t[0]}" title="${t[1]}" aria-label="${t[1]}">${t[2]}</button>`).join('');
    const picker = document.createElement('input');
    picker.type = 'file';
    picker.accept = 'image/*';
    picker.hidden = true;
    bar.appendChild(picker);
    // Pressing a button must not take the selection away from the preview before it is read
    bar.addEventListener('mousedown', e => { if(e.target.closest('.tool')) e.preventDefault(); });
    bar.addEventListener('click', e => {
      const button = e.target.closest('[data-tool]');
      const tool = button && tools.find(t => t !== '|' && t[0] === button.dataset.tool);
      if(tool) tool[3](button);
    });
    area.addEventListener('keydown', e => {
      if(!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
      const key = e.key.toLowerCase();
      if(key === 'b'){ e.preventDefault(); wrap('**'); }
      else if(key === 'i'){ e.preventDefault(); wrap('*'); }
      else if(key === 'k'){ e.preventDefault(); link(); }
    });

    /* The edit: what `change` makes of the text and the selection, put in as typing would be */
    function edit(change){
      const value = area.value;
      const placed = previewRange(preview, value);
      const from = placed ? placed.start : area.selectionStart;
      const to = placed ? placed.end : area.selectionEnd;
      const next = change(value, from, to);
      let head = 0;
      while(head < value.length && head < next.text.length && value[head] === next.text[head]) head++;
      let tail = 0;
      while(tail < value.length - head && tail < next.text.length - head
            && value[value.length - 1 - tail] === next.text[next.text.length - 1 - tail]) tail++;
      const looking = area.scrollTop;
      // The preview's selection is used up: pressed again before the preview is drawn anew, it
      // would point at text that has moved
      if(placed) window.getSelection().removeAllRanges();
      area.focus({preventScroll: true});
      area.setSelectionRange(head, value.length - tail);
      const typed = next.text.slice(head, next.text.length - tail);
      let done = false;
      try{ done = document.execCommand(typed ? 'insertText' : 'delete', false, typed); }catch(e){}
      if(!done || area.value !== next.text){
        area.value = next.text;
        area.dispatchEvent(new Event('input', {bubbles: true}));
      }
      area.setSelectionRange(next.from, next.to);
      area.scrollTop = looking;
    }

    /* Marks around the selection, or taken off when they are already there */
    function wrap(mark, end = mark){
      edit((text, from, to) => {
        if(text.slice(from - mark.length, from) === mark && text.slice(to, to + end.length) === end)
          return {text: text.slice(0, from - mark.length) + text.slice(from, to) + text.slice(to + end.length),
                  from: from - mark.length, to: to - mark.length};
        const inner = text.slice(from, to);
        if(inner.startsWith(mark) && inner.endsWith(end) && inner.length >= mark.length + end.length)
          return {text: text.slice(0, from) + inner.slice(mark.length, inner.length - end.length) + text.slice(to),
                  from, to: to - mark.length - end.length};
        return {text: text.slice(0, from) + mark + inner + end + text.slice(to), from: from + mark.length, to: to + mark.length};
      });
    }

    /* A prefix on every line the selection touches, off again when every line has it */
    function prefix(mark, numbered = false){
      edit((text, from, to) => {
        const start = lineStart(text, from), end = lineEnd(text, to);
        const lines = text.slice(start, end).split('\n');
        const has = line => numbered ? /^\d+\. /.test(line) : line.startsWith(mark);
        const already = lines.every(has);
        const body = lines.map((line, i) => already ? line.replace(numbered ? /^\d+\. / : mark, '')
                                                    : (numbered ? `${i + 1}. ` : mark) + line).join('\n');
        return {text: text.slice(0, start) + body + text.slice(end), from: start, to: start + body.length};
      });
    }

    /* A heading of this level, in place of any other; the same level again takes it off */
    function heading(level){
      edit((text, from, to) => {
        const start = lineStart(text, from), end = lineEnd(text, to);
        const mark = '#'.repeat(level) + ' ';
        const lines = text.slice(start, end).split('\n');
        const already = lines.every(line => line.startsWith(mark));
        const body = lines.map(line => already ? line.slice(mark.length) : mark + line.replace(/^#{1,6} /, '')).join('\n');
        return {text: text.slice(0, start) + body + text.slice(end), from: start, to: start + body.length};
      });
    }

    /* A block on lines of its own, the caret `caret` characters into it */
    function insert(block, caret = block.length){
      edit((text, from, to) => {
        const before = text.slice(0, from);
        const gap = before === '' || before.endsWith('\n\n') ? '' : before.endsWith('\n') ? '\n' : '\n\n';
        return {text: before + gap + block + text.slice(to), from: from + gap.length + caret, to: from + gap.length + caret};
      });
    }

    /* [words](address), with the address picked to be typed over */
    function link(){
      edit((text, from, to) => {
        const words = text.slice(from, to) || 'link text';
        const made = `[${words}](https://)`;
        const at = from + words.length + 3;
        return {text: text.slice(0, from) + made + text.slice(to),
                from: from === to ? from + 1 : at, to: from === to ? from + 1 + words.length : at + 8};
      });
    }

    /* The lines the selection touches, aligned - as MDViewer writes it, a <div align> around them,
       the one piece of HTML the gateway lets through. Pressed again on aligned lines, taken off. */
    function align(side){
      edit((text, from, to) => {
        const start = lineStart(text, from), end = lineEnd(text, to);
        const around = /<div align="(left|center|right)">\n\n$/.exec(text.slice(0, start));
        const closes = text.slice(end).startsWith('\n\n</div>');
        if(around && closes){
          const open = start - around[0].length;
          const inner = text.slice(start, end);
          const keep = around[1] === side ? inner : `<div align="${side}">\n\n${inner}\n\n</div>`;
          return {text: text.slice(0, open) + keep + text.slice(end + '\n\n</div>'.length),
                  from: open + (around[1] === side ? 0 : around[0].length), to: open + (around[1] === side ? inner.length : around[0].length + inner.length)};
        }
        const inner = text.slice(start, end);
        const block = `<div align="${side}">\n\n${inner}\n\n</div>`;
        return {text: text.slice(0, start) + block + text.slice(end),
                from: start + block.indexOf(inner), to: start + block.indexOf(inner) + inner.length};
      });
    }

    /* A new chart on lines of its own, with its title picked to be typed over */
    function insertChart(type){
      edit((text, from) => {
        const ls = lineStart(text, from), le = lineEnd(text, from);
        const at = text.slice(ls, le).trim() === '' ? ls : le;
        const before = text.slice(0, at), after = text.slice(at);
        const head = before === '' || before.endsWith('\n\n') ? '' : before.endsWith('\n') ? '\n' : '\n\n';
        const tail = after === '' || after.startsWith('\n') ? '' : '\n';
        const opening = '```chart\ntype: ' + type + '\ntitle: ';
        const chart = opening + 'Chart title\n' + chartBody(type) + '\n```\n';
        const title = at + head.length + opening.length;
        return {text: before + head + chart + tail + after, from: title, to: title + 'Chart title'.length};
      });
    }

    let menu = null;
    function closeMenu(){
      menu?.remove();
      menu = null;
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('keydown', escape, true);
    }
    const outside = e => { if(menu && !menu.contains(e.target) && !e.target.closest('.tool-chart')) closeMenu(); };
    const escape = e => { if(e.key === 'Escape') closeMenu(); };
    /* The picker, placed against the window rather than the strip, which scrolls sideways on a
       narrow screen and would clip it; pulled back inside the window once it is measured */
    function pickChart(button){
      if(menu){ closeMenu(); return; }
      menu = document.createElement('div');
      menu.className = 'chart-picker';
      menu.setAttribute('role', 'menu');
      menu.innerHTML = '<p class="chart-picker-heading">Insert chart</p>' + FORMS.map(([type, name, purpose]) =>
        `<button type="button" role="menuitem" data-form="${type}"><span class="chart-picker-name">${name}</span>
         <span class="chart-picker-purpose">${purpose}</span></button>`).join('');
      document.body.appendChild(menu);
      const box = button.getBoundingClientRect(), size = menu.getBoundingClientRect();
      menu.style.left = Math.max(8, Math.min(box.left, innerWidth - size.width - 8)) + 'px';
      menu.style.top = Math.max(8, Math.min(box.bottom + 6, innerHeight - size.height - 8)) + 'px';
      menu.addEventListener('mousedown', e => e.preventDefault());
      menu.addEventListener('click', e => {
        const form = e.target.closest('[data-form]');
        if(!form) return;
        closeMenu();
        insertChart(form.dataset.form);
      });
      setTimeout(() => {
        document.addEventListener('mousedown', outside, true);
        document.addEventListener('keydown', escape, true);
      });
    }

    picker.addEventListener('change', async () => {
      const file = picker.files[0];
      picker.value = '';
      if(!file) return;
      const button = bar.querySelector('.tool-image');
      button.disabled = true;
      try{
        const stored = await upload(file);
        const alt = (stored.name || file.name || 'picture').replace(/\.[^.]+$/, '').replace(/[\[\]]/g, '');
        insert(`![${alt}](${stored.url})\n`);
      }catch(err){
        alert('The picture could not be added: ' + (err.message || err));
      }finally{ button.disabled = false; }
    });

    return {close: closeMenu};
  }

  window.mdToolbar = {attach, drawCharts, chartsOf};
})();
