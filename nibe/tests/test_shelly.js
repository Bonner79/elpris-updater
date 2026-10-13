// Logic simulation only. NOT the real Shelly interpreter, GPIO or NIBE firmware.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '..', 'shelly_nibe.js'), 'utf8');
const BASE = Date.parse('2026-10-11T12:00:00+02:00') / 1000;
const DAY = Date.parse('2026-10-11T00:00:00+02:00') / 1000;
let passed = 0;
function test(name, fn) { fn(); passed++; console.log('PASS', name); }
function rig() {
  const r = { now: BASE, up: 100, outputs: [false,false,false], expiry: [0,0,0],
    calls: [], pending: [], cache: {}, failOff: -1, http: null, callbacks: [] };
  r.cfg = [0,1,2].map(id => ({id, in_mode:'detached', initial_state:'off', auto_on:false, auto_off:true, auto_off_delay:120}));
  const context = {
    print(){}, Date, JSON, Math, String, Number, Array,
    Timer: { set(ms, repeat, fn){ r.callbacks.push(fn); return r.callbacks.length; } },
    Script: { storage: { getItem(k){return r.cache[k] || null;}, setItem(k,v){r.cache[k]=v;} } },
    Shelly: {
      getComponentStatus(key) {
        if (key==='sys') return {unixtime:r.now, uptime:r.up};
        const i=Number(key.split(':')[1]);
        return {output:r.outputs[i]};
      },
      getComponentConfig(key) { return r.cfg[Number(key.split(':')[1])]; },
      call(method, params, cb) {
        r.calls.push({method, ...params});
        if (method === 'Switch.Set') {
          const error = params.on === false && params.id === r.failOff;
          if (!error) {
            r.outputs[params.id] = params.on;
            r.expiry[params.id] = params.on ? r.now + (params.toggle_after || 120) : 0;
          }
          r.pending.push(() => cb({}, error ? 1 : 0, error ? 'simulated failure' : undefined));
        } else {
          r.pending.push(() => cb(r.http, r.http ? 0 : 1, 'mock HTTP'));
        }
      }
    }
  };
  r.ctx=vm.createContext(context);
  r.flush=()=>{let i=0;while(r.pending.length){if(++i>100)throw Error('callback loop');r.pending.shift()();}};
  r.run=(expr)=>{const v=vm.runInContext(expr,r.ctx);r.flush();return v;};
  r.advance=(secs, tick=true)=>{
    r.now+=secs; r.up+=secs;
    for(let i=0;i<3;i++) if(r.expiry[i] && r.expiry[i]<=r.now) {r.outputs[i]=false;r.expiry[i]=0;}
    if(tick) r.run('tick()');
  };
  vm.runInContext(source,r.ctx);r.flush();
  r.load=(p)=>r.run('installPlan('+JSON.stringify(JSON.stringify(p))+')');
  r.live=()=>r.run('CONFIG.dry_run=false;tick()');
  return r;
}
function payload(water=[BASE-60,BASE+3600],heat=[]) {
  return {system:'nibe-aux-v1',area:'SE3',generated:BASE-3600,
    days:[{date:'2026-10-11',start:DAY,end:DAY+86400,water,heat}]};
}
test('preview sends no relay commands',()=>{let r=rig();r.load(payload());r.run('tick()');assert.equal(r.calls.filter(x=>x.method==='Switch.Set').length,0);});
test('low-price maps to physical relay 3 / id 2',()=>{let r=rig();r.load(payload());r.live();assert.deepEqual(r.outputs,[false,false,true]);});
test('no plan means no extra requests',()=>{let r=rig();r.outputs=[true,true,true];r.live();assert.deepEqual(r.outputs,[false,false,false]);});
test('expired plan not used',()=>{let r=rig();r.load(payload([BASE-120,BASE-60]));r.live();assert.deepEqual(r.outputs,[false,false,false]);});
test('window end releases relay',()=>{let r=rig();r.load(payload([BASE-60,BASE+20]));r.live();r.advance(20);assert.equal(r.outputs[2],false);});
test('lease holds only while controller renews',()=>{let r=rig();r.load(payload());r.live();for(let i=0;i<8;i++)r.advance(30);assert.equal(r.outputs[2],true);r.advance(91,false);assert.equal(r.outputs[2],false);});
test('heat disabled by default',()=>{let r=rig();r.load(payload([],[BASE-60,BASE+3600]));r.live();assert.equal(r.outputs[0],false);});
test('optional heat maps to id 0',()=>{let r=rig();r.load(payload([],[BASE-60,BASE+3600]));r.run('CONFIG.allow_heat=true');r.live();assert.deepEqual(r.outputs,[true,false,false]);});
test('overlapping producer requests rejected',()=>{let r=rig();assert.equal(r.load(payload([BASE-60,BASE+200],[BASE-30,BASE+100])),false);});
test('manual luxury disabled by default',()=>{let r=rig();r.live();assert.match(r.run('boost(60)'),/Not permitted/);});
test('manual luxury opens SG before closing luxury',()=>{
  let r=rig();r.load(payload());r.run('CONFIG.allow_luxury=true');r.live();r.calls=[];r.run('boost(60);tick()');
  assert.deepEqual(r.outputs,[false,true,false]);
  const a=r.calls.filter(x=>x.method==='Switch.Set');assert.equal(a[0].id,2);assert.equal(a[0].on,false);assert.equal(a[1].id,1);assert.equal(a[1].on,true);
});
test('failed SG OFF prevents luxury ON',()=>{
  let r=rig();r.load(payload());r.run('CONFIG.allow_luxury=true');r.live();r.failOff=2;r.calls=[];r.run('boost(60);tick()');
  assert.equal(r.outputs[1],false);assert(!r.calls.some(x=>x.id===1&&x.on===true));
});
test('luxury cancellation resumes valid SG interval',()=>{let r=rig();r.load(payload());r.run('CONFIG.allow_luxury=true');r.live();r.run('boost(60);tick()');r.run('boost(0);tick()');assert.deepEqual(r.outputs,[false,false,true]);});
test('unknown clock releases contacts',()=>{let r=rig();r.load(payload());r.live();r.now=null;r.run('tick()');assert.deepEqual(r.outputs,[false,false,false]);});
test('clock jump suspends extra requests',()=>{let r=rig();r.load(payload());r.live();r.now+=100;r.up+=5;r.run('tick()');assert.deepEqual(r.outputs,[false,false,false]);});
test('invalid JSON does not erase a valid plan',()=>{let r=rig();r.load(payload());assert.equal(r.run('installPlan("not json")'),false);r.live();assert.equal(r.outputs[2],true);});
test('wrong area rejected',()=>{let r=rig();let p=payload();p.area='SE4';assert.equal(r.load(p),false);});
test('future-dated generation rejected',()=>{let r=rig();let p=payload();p.generated=BASE+3600;assert.equal(r.load(p),false);});
test('startup reads persistent plan',()=>{let r=rig();r.cache.nibe_plan_v1=JSON.stringify(payload());r.run('cacheLoaded=false;plan=null;tick()');r.live();assert.equal(r.outputs[2],true);});
test('unsafe auto-on config prevents extra requests',()=>{let r=rig();r.load(payload());r.cfg[2].auto_on=true;r.live();assert.deepEqual(r.outputs,[false,false,false]);});
test('no midnight fallback and no valid future plan today',()=>{
  let r=rig();let p=payload();p.days[0]={date:'2026-10-12',start:DAY+86400,end:DAY+172800,heat:[],water:[DAY+90000,DAY+93600]};
  r.load(p);r.live();assert.deepEqual(r.outputs,[false,false,false]);
});
test('pause releases contacts; resume restores only valid periods',()=>{let r=rig();r.load(payload());r.live();r.run('pauseControl();tick()');assert.equal(r.outputs[2],false);r.run('resumeControl();tick()');assert.equal(r.outputs[2],true);});
test('redirect cannot send device to arbitrary host',()=>{let r=rig();assert.equal(r.run('permittedURL("http://192.168.1.1")'),false);});
console.log('TOTAL:',passed,'controller logic tests passed; hardware not tested.');
