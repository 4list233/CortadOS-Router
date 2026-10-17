// Test-only external quota boundary. Real native credential/config code remains active.
const fs = require('fs'), Module = require('module'), path = require('path');
const append = fs.appendFileSync.bind(fs);
const home = process.env.QUOTA_NATIVE_EXAM_HOME;
// Freeze only the fixture clock so recorded native geometry and exact expiry boundaries are reproducible.
Date.now=()=>Number(fs.readFileSync(path.join(home,'observed-at.json'),'utf8'))*1000;
const log = (kind) => append(path.join(home, 'native-calls.jsonl'), JSON.stringify({kind})+'\n');
const reject = () => { log('NETWORK_FORBIDDEN'); throw Error('Independent fixture forbids network'); };
for (const name of ['http','https']) {
  const m=require(name); m.request=reject; m.get=reject;
}
for (const name of ['net','tls']) {
  const m=require(name); m.connect=reject; m.createConnection=reject;
}
require('net').Socket.prototype.connect=reject;
globalThis.fetch=reject;
const compile = Module.prototype._compile;
Module.prototype._compile=function(source, filename) {
  const result=compile.call(this,source,filename);
  if (path.basename(filename)==='index.js' && typeof this.exports==='function' && source.includes('./src/dashboard-client.ts')) {
    const nativeRequire=this.exports;
    const wrapped=function(id) {
      const value=nativeRequire(id);
      if (id!=='./src/dashboard-client.ts') return value;
      return {...value,m:()=>({getCurrentPeriodUsage:async()=>{
        log('dashboard-quota');
        const payload=JSON.parse(fs.readFileSync(path.join(home,'quota.json'),'utf8'));
        return {toJson:()=>payload};
      }})};
    };
    Object.assign(wrapped,nativeRequire);
    this.exports=wrapped;
  }
  return result;
};
