const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {webcrypto, createHash} = require('node:crypto');
const source = fs.readFileSync('app/static/js/edit.js', 'utf8');
const extract = name => source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))[0];
const hash = data => createHash('sha256').update(data).digest('hex');
const sessions = new Map(), tokens = new Map(), sent = [];
let interrupt = true, rejectStart = false;
const context = {
  Blob, FormData, URLSearchParams, TextEncoder, crypto: webcrypto,
  processedBlob: new Blob(['abcdef']), SEGMENT_SIZE: 4, SVS_BASE: '',
  titleInput: {value: 'same title'}, folderInput: {value: ''},
  descriptionInput: {value: ''}, coverBlob: null,
  selectedTagIds: () => [], setProgress() {}, clientLog() {}, addMeta() {}, addTags() {},
  $: () => ({classList: {add() {}}}),
  loadResumeToken: key => tokens.get(key) || '',
  saveResumeToken: (key, token) => tokens.set(key, token),
  clearResumeToken: key => tokens.delete(key),
  retryUpload: op => op(),
  fetch: async (url, options) => {
    const fields = options.body;
    if (rejectStart) return {status: 400, json: async () => ({error: 'Rejected'})};
    const fp = fields.get('fingerprint');
    assert.match(fp, /^[a-f0-9]{64}$/);
    const bytes = Buffer.from(await context.processedBlob.arrayBuffer());
    const hashes = [];
    for (let i = 0; i < bytes.length; i += 4) hashes.push(hash(bytes.subarray(i, i + 4)));
    assert.equal(fp, hash(hashes.join('')));
    let token = fields.get('resume_token');
    if (!token) {
      token = `token-${sessions.size}`;
      sessions.set(token, {fp, uploaded: []});
    }
    assert.equal(sessions.get(token).fp, fp);
    return {status: 200, json: async () => ({ok: true, token, uploaded_parts: sessions.get(token).uploaded})};
  },
  xhrPost: async (url, form) => {
    if (url.endsWith('/part')) {
      const index = Number(form.get('part_index'));
      assert.equal(form.get('sha256'), hash(Buffer.from(await form.get('part').arrayBuffer())));
      if (interrupt && index === 1) { interrupt = false; throw new Error('interrupted'); }
      sent.push(index);sessions.get(form.get('token')).uploaded.push(index);
    }
  },
  makeProgressCb: () => () => {},
  fmtMB: String,
};
vm.createContext(context);
vm.runInContext(extract('resumeKey') + '\n' + extract('segmentedUpload'), context);
(async () => {
  await assert.rejects(context.segmentedUpload(), /interrupted/);
  assert.deepEqual(sent, [0]);
  assert.equal(tokens.size, 1);
  await context.segmentedUpload();
  assert.deepEqual(sent, [0, 1]);
  assert.equal(sessions.size, 1, 'retry must resume the same file');
  assert.equal(tokens.size, 0);
  context.processedBlob = new Blob(['uvwxyz']);
  await context.segmentedUpload();
  assert.equal(sessions.size, 2, 'same-size different file must have a distinct identity');
  rejectStart = true;
  await assert.rejects(context.segmentedUpload(), err => err.status === 400);
  console.log('Editor segmented upload identity, part checksums, resume and rejection tests passed');
})().catch(err => {console.error(err);process.exitCode = 1;});
