const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const read = name => fs.readFileSync(`app/static/js/${name}.js`, 'utf8');
const extract = (source, name) => source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))[0];

for (const name of ['upload', 'edit']) {
  const source = read(name);
  let handler;
  const context = vm.createContext({processing: false, activeUploadRetries: 0,
    window: {addEventListener: (_, callback) => {handler = callback;}},
    cancelActiveTranscode() {throw new Error('Canceling the leave prompt must preserve the running task');},
  });
  vm.runInContext(source.match(/window.addEventListener\("beforeunload", \(event\) => \{[^]*?\n\}\);/)[0], context);
  for (const [processing, retries, expected] of [[false,0,false],[true,0,true],[false,1,true],[false,2,true],[false,0,false]]) {
    Object.assign(context, {processing, activeUploadRetries: retries});
    const event = {prevented: false, preventDefault() {this.prevented = true;}};
    handler(event);
    assert.equal(event.prevented, expected, name);
    if (expected) assert.equal(event.returnValue, '');
  }
  const bitrateBlock = source.match(/if \(maxBitrateInput\) \{[^]*?\n\}/)[0];
  for (const [savedAdmin, saved, current, expected] of [[5000,3000,6000,6000],[6000,3000,6000,3000],[null,3000,6000,6000],[6000,5000,2000,2000]]) {
    const values = new Map([['svs-max-bitrate',String(saved)]]);
    if (savedAdmin !== null) values.set('svs-max-bitrate-admin',String(savedAdmin));
    let change;
    const input = {value: current, addEventListener: (_, cb) => {change = cb;}};
    vm.runInNewContext(bitrateBlock, {maxBitrateInput: input, MAX_BITRATE_KBPS: current,
      window: {SVS_MAX_BITRATE_KBPS: current},
      localStorage: {getItem: key => values.get(key) ?? null, setItem: (key,value) => values.set(key,value)},
    });
    assert.equal(Number(input.value), expected, name);
    input.value = '1200';change();
    assert.equal(values.get('svs-max-bitrate-admin'), String(current));
  }
}

(async () => {
  for (const name of ['pipeline', 'edit']) {
    const source = read(name), file = new Blob(['source']), output = new Blob(['converted']);
    const calls = [];
    let fallback = false;
    const probe = {codec: 'avc', height:720, fps:30, bitrateKbps:1000};
    const context = vm.createContext({Blob, MAX_HEIGHT:1080, MAX_BITRATE_KBPS:5000,
      probeInfo: probe, sourceFile:file, activeTranscodeController:null, AbortController,
      getMaxFps:()=>60, getMaxBitrate:()=>5000, clipRange:()=>null,
      $:()=>({classList:{add(){}, remove(){}}}), clientLog(){}, setProgress(){}, fmtMB:String,
      cancelActiveTranscode(){}, failTranscode:message=>new Error(message),
      mediabunnyUnavailableReason:async()=>fallback ? 'unavailable' : null,
      loadMediabunny:async()=>({canEncode:async codec=>{assert.equal(codec,'avc');return true;}}),
      runMediabunny:async (...args)=>{calls.push(args[1].codec);return output;},
      runFfmpeg:async (...args)=>{const opts=name==='pipeline'?args[1]:args[0];calls.push(opts.encoder);return output;},
    });
    vm.runInContext(source.match(/const WEBCODECS_CODEC_PRIORITY = [^;]+;/)[0] + '\n' +
      (name==='edit' ? extract(source,'needsTranscode') : '') + '\n' + extract(source,'processVideo'), context);
    const run = () => name==='pipeline'
      ? context.processVideo(file,{probe, maxFps:60},{log(){}, setProgress(){}})
      : context.processVideo();
    const direct = await run();
    assert.equal(name==='pipeline'?direct.blob:direct,file);
    probe.codec = 'av1';await run();assert.deepEqual(calls,['avc']);
    fallback = true;await run();assert.deepEqual(calls,['avc','libx264']);
    assert.doesNotMatch(source,/encoder: "libsvtav1"/);
  }
  console.log('H264 output/direct-pass policy, leave warnings and admin bitrate changes passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
