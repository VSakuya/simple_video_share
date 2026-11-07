const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const MiB=1024*1024;
const extract=(source,name)=>source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))[0];
class VirtualBlob {
  constructor(size){this.size=size;}
  slice(start,end){return new VirtualBlob(Math.min(end,this.size)-start);}
  async arrayBuffer(){return new Uint8Array([this.size%256]).buffer;}
}
class Form {constructor(){this.fields=new Map();} append(k,v){this.fields.set(k,v);} get(k){return this.fields.get(k);}}
(async()=>{
  for(const name of ['pipeline','edit']) {
    const source=fs.readFileSync(`app/static/js/${name}.js`,'utf8');
    for(const [size,singleFails,expectedParts] of [[64*MiB,false,0],[64*MiB+1,false,2],[Math.round(255.2*MiB),false,4],[32*MiB,true,1]]) {
      const parts=[], endpoints=[]; let failPart=true;
      const context=vm.createContext({processedBlob:new VirtualBlob(size),FormData:Form,URLSearchParams,TextEncoder,crypto:webcrypto,
        SEGMENT_RETRY_MAX:3, SEGMENT_RETRY_BASE_MS:0, SVS_BASE:'',
        titleInput:{value:'fixture'},folderInput:{value:''},descriptionInput:{value:''},coverBlob:null,
        appendTags(){},addTags(){},addMeta(){},setProgress(){},clientLog(){},fmtMB:String,
        selectedTagIds:()=>[],loadResumeToken:()=>'',saveResumeToken(){},clearResumeToken(){},
        $:()=>({classList:{add(){}}}),makeProgressCb:()=>()=>{},sleep:async()=>{},
        fetch:async(url,options)=>{endpoints.push(url);assert.match(options.body.get('fingerprint'),/^[a-f0-9]{64}$/);return{status:200,json:async()=>({ok:true,token:'fixture',uploaded_parts:[]})};},
        xhrPost:async(url,form)=>{
          endpoints.push(url);
          if(url.endsWith('/submit')&&singleFails)throw Object.assign(new Error('Upload failed (500)'),{status:500});
          if(url.endsWith('/part')){
            if(failPart){failPart=false;throw Object.assign(new Error('temporary failure'),{status:500});}
            parts.push(form.get('part').size);assert.match(form.get('sha256'),/^[a-f0-9]{64}$/);
          }
        },
      });
      const constants=source.match(/const SEGMENT_SIZE = [^;]+;/)[0]+'\n'+source.match(/const SEGMENT_THRESHOLD = [^;]+;/)[0];
      vm.runInContext(constants+'\n'+extract(source,'isRetriableUploadError')+'\n'+extract(source,'retryUpload')+'\n'+extract(source,'resumeKey')+'\n'+
        (name==='pipeline'?extract(source,'uploadVideo'):extract(source,'singleUpload')+'\n'+extract(source,'segmentedUpload')),context);
      if(name==='pipeline')await context.uploadVideo(context.processedBlob,{title:'fixture'},{log(){},setProgress(){}});
      else await vm.runInContext('processedBlob.size <= SEGMENT_THRESHOLD ? singleUpload() : segmentedUpload()',context);
      assert.equal(parts.length,expectedParts,name);
      if(expectedParts){assert.equal(parts.reduce((a,b)=>a+b,0),size);assert.ok(parts.every(p=>p<=64*MiB));}
      if(size>64*MiB)assert.ok(!endpoints.includes('/upload/submit'));
      if(singleFails)assert.ok(endpoints.includes('/upload/seg/start'));
    }
  }
  console.log('64 MiB boundaries, 255.2 MiB segmentation, single-500 fallback and part retry passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
