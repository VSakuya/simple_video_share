const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('app/static/js/pipeline.js', 'utf8');
const retry = source.match(/function isRetriableUploadError\(err\) \{[\s\S]*?\n\}/)[0];
vm.runInNewContext(retry + `
  for (const status of [408,409,429,500,502,503,504]) assert.equal(isRetriableUploadError({status}), true);
  for (const status of [400,401,403,404,413]) assert.equal(isRetriableUploadError({status}), false);
  assert.equal(isRetriableUploadError(new TypeError('Failed to fetch')), true);
  assert.equal(isRetriableUploadError(new Error('checksum mismatch')), false);
`, {assert});
console.log('Upload retry classification passed');
