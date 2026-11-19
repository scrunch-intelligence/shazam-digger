// Runs the picks page's own script against a stand-in page (no browser needed) and checks what the buttons do.
// Used by test_digger.py (skipped when node is not installed):  node test_page.js out/picks.html
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const src = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];
const els = {}, store = {}, downloads = [], listeners = {};
const mk = id => ({id, dataset: {}, style: {}, children: [], classList: {add() {}, remove() {}}, hidden: false, paused: true, innerHTML: '', textContent: '', value: '',
  setAttribute() {}, removeAttribute() {}, append(...a) { this.children.push(...a) }, appendChild(a) { this.children.push(a) }, replaceChildren() { this.children = [] },
  focus() {}, select() {}, remove() {}, click() {}, scrollIntoView() {}, addEventListener() {}, getBoundingClientRect() { return {left: 0, width: 100} },
  play() { return Promise.resolve() }, pause() {}, contains() { return false }, closest() { return null }, offsetHeight: 100});
const doc = {getElementById: id => els[id] || (els[id] = mk(id)), createElement: t => mk(t), addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f) },
  querySelector: () => mk('q'), body: {appendChild() {}}, documentElement: {style: {setProperty() {}}}, activeElement: null, execCommand: () => false};
const ctx = {document: doc, localStorage: {getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v }}, location: {pathname: '/t'},
  navigator: {clipboard: {writeText: t => { ctx.copied = t; return Promise.resolve() }}}, ResizeObserver: class { observe() {} },
  URL: {createObjectURL: b => { downloads.push(b.text); return 'blob:x' }}, Blob: class { constructor(p) { this.text = p.join('') } },
  confirm: () => true, setTimeout: () => 0, clearTimeout() {}, console};
vm.createContext(ctx); vm.runInContext(src, ctx);
const ev = s => vm.runInContext(s, ctx);
const click = dataset => { const b = {dataset, closest: () => b}; listeners.click.forEach(f => f({target: b})) };

// no instruction text on the page itself: it all lives in the gear drop-down
assert(!/Press Start|Export your keeps|Songs you Shazamed more than once/.test(html.replace(/<nav class="dd"[\s\S]*?<\/nav>/, '')), 'instruction text outside the gear');
assert(/<nav class="dd"/.test(html) && /id="gear"/.test(html), 'gear drop-down missing');
// a release date on every card; older than 8 weeks gets a chip
const list = els.list.innerHTML, cards = (list.match(/class="row /g) || []).length;
assert(cards >= 1, 'no cards');
assert.strictEqual((list.match(/chip rel/g) || []).length, cards, 'every card shows a release line');
assert(/older release/.test(list) && /Released 1 Aug 2000/.test(list), 'old release chip');
assert(!/Store<|Record pool</.test(list), 'link names only appear when a menu is open');
// Can't play here
assert.strictEqual(els.nf.hidden, false); assert(/\(1\)/.test(els.nfs.textContent));
els.nfc.onclick(); assert.strictEqual(ctx.copied, 'Fatboy Slim - Praise You', 'Copy all gives plain Artist - Title lines');
// I have it: in the More menu, saved as 'have', not exported as a keep, counted, never dropped by Clear heard
const T = ev('T'); const id0 = T[0].id, id1 = T[1].id;
click({more: '0'}); assert(/data-pick="have"/.test(els.list.innerHTML) && />I have it</.test(els.list.innerHTML), 'I have it lives in More');
click({pick: 'have', i: '0'});
assert.strictEqual(ev('picks')[id0], 'have');
assert(/1<\/b> have/.test(els.tally.innerHTML), 'tally counts have');
click({pick: 'keep', i: '1'});
els.exTxt.onclick(); els.exPicks.onclick();
assert(downloads[0].includes(T[1].title) && !downloads[0].includes(T[0].title), 'keeps export holds keeps only');
assert(downloads[1].includes(`"${T[0].artist}","${T[0].title}","have"`), 'picks export holds have');
ev("heardAdd(T[0].id); heardAdd(T[2] ? T[2].id : T[1].id)");
const b = ev('batchList()'); assert(b.stay.includes(id0), 'Clear heard keeps I have it');
ev('batchDrop()'); assert.strictEqual(ev('picks')[id0], 'have');
// undo from the toast puts it back on the list
ev("picks[T[0].id]='have'"); ev("showToast('x',()=>{delete picks[T[0].id]})"); ev('toastFn()'); assert(!ev('picks')[id0]);
console.log('page ok');
