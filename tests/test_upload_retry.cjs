const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
for (const page of ['pipeline', 'edit']) {
const source = fs.readFileSync(`app/static/js/${page}.js`, 'utf8');
const retry = source.match(/function isRetriableUploadError\(err\) \{[\s\S]*?\n\}/)[0];
vm.runInNewContext(retry + `
  for (const status of [408,409,429,500,502,503,504]) assert.equal(isRetriableUploadError({status}), true);
  for (const status of [400,401,403,404,413]) assert.equal(isRetriableUploadError({status}), false);
  assert.equal(isRetriableUploadError(new TypeError('Failed to fetch')), true);
  assert.equal(isRetriableUploadError(new Error('checksum mismatch')), false);
`, {assert});
const xhrPost = source.match(/function xhrPost\([^]*?\n\}/)[0];
let status = 500;
const context = vm.createContext({XMLHttpRequest: class {
  constructor() {this.upload = {};this.status = status;this.responseText = '';}
  open() {} setRequestHeader() {} send() {this.onload();}
}});
vm.runInContext(xhrPost, context);
const rejection = context.xhrPost('/upload/submit', {}, () => {});
rejection.then(() => {throw new Error('500 unexpectedly accepted');}, error => assert.equal(error.status, 500));
}
console.log('Upload retry classification passed');
