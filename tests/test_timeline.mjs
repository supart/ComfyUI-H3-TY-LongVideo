import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fitWindow, zoomWindow, pcmWavePeaks, createTimeline} from '../web/timeline.js';

test('long-song zoom keeps the pointer time anchored', () => {
  const view={start:0,span:209.269};
  const next=zoomWindow(view,.5,175,209.269);
  assert.ok(Math.abs((175-next.start)/next.span-175/view.span)<1e-10);
  assert.deepEqual(fitWindow(205,8,209.269),{start:201.269,span:8});
  assert.deepEqual(fitWindow(-4,8,209.269),{start:0,span:8});
  assert.equal(zoomWindow({start:178,span:8},.001,182,209.269).span,.5);
});
function wav(samples, channels=2) {
  const buffer=new ArrayBuffer(44+samples.length*2),v=new DataView(buffer);
  const tag=(offset,text)=>[...text].forEach((c,i)=>v.setUint8(offset+i,c.charCodeAt(0)));
  tag(0,'RIFF');v.setUint32(4,buffer.byteLength-8,true);tag(8,'WAVE');tag(12,'fmt ');
  v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,channels,true);
  v.setUint32(24,48000,true);v.setUint32(28,48000*channels*2,true);v.setUint16(32,channels*2,true);v.setUint16(34,16,true);
  tag(36,'data');v.setUint32(40,samples.length*2,true);
  samples.forEach((s,i)=>v.setInt16(44+i*2,s,true));return buffer;
}
test('detail peaks retain silence, negative samples and both channels',()=>{
  assert.deepEqual([...pcmWavePeaks(wav([0,0,-32768,0,0,0,0,16384]),4)],[0,1,0,1]);
  assert.deepEqual([...pcmWavePeaks(wav([0,0,0,0]),2)],[0,0]);
  assert.throws(()=>pcmWavePeaks(new ArrayBuffer(10)));
  assert.throws(()=>pcmWavePeaks(wav([1,2]).slice(0,45)));
});
test('late-segment selection, fine drag and stale detail requests',async()=>{
  const ctx=new Proxy({}, {get:(_,name)=>name==='measureText'?()=>({width:36}):()=>{}});
  class Node {
    children=[]; value='0';
    append(n){this.children.push(n);} before(n){this.tools=n;} after(n){this.afterNode=n;}
    setAttribute(){} addEventListener(name,fn){this[name]=fn;}
    getBoundingClientRect(){return {left:0,width:1000};} getContext(){return ctx;}
    setPointerCapture(){} hasPointerCapture(){return false;}
  }
  globalThis.document={createElement:()=>new Node()};globalThis.window={devicePixelRatio:1};
  globalThis.ResizeObserver=class{observe(){} disconnect(){}};
  const canvas=new Node(),ends=Array.from({length:17},(_,i)=>Math.min((i+1)*12.3,209.1));
  let selected=0,commits=0;
  const plan={id:'song',duration:209.1,max_seconds:15,segments:ends.map(()=>({boundary_confidence:.8}))};
  const rows=ends.map(end=>({end:{value:String(end)}}));let requests=[];
  createTimeline(canvas,()=>({plan,rows,selected,analysis:{}}),i=>selected=i,(i,t)=>rows[i].end.value=t,()=>commits++,
    (start,end,signal)=>new Promise(resolve=>requests.push({start,end,signal,resolve})));
  canvas.drawTimeline();
  const evt=(x)=>({clientX:x,button:0,pointerId:1});
  canvas.onpointerdown(evt(50+178/209.1*932));canvas.onpointerup(evt(0));assert.equal(selected,14);assert.equal(commits,0);
  canvas.tools.children[4].onclick();await new Promise(r=>setTimeout(r,180));
  assert.equal(requests[0].end-requests[0].start,8);
  canvas.onpointerdown(evt(516));canvas.onpointermove(evt(518));canvas.onpointerup(evt(518));
  assert.ok(Math.abs(Number(rows[14].end.value)-ends[14]-16/932)<1e-8);assert.equal(commits,1);
  canvas.tools.children[2].onclick();await new Promise(r=>setTimeout(r,180));assert.ok(requests[0].signal.aborted);
  requests[0].resolve({original:[],vocals:[]});await Promise.resolve();
  assert.match(canvas.tools.children.at(-1).textContent,/加载中/);
  requests[1].resolve({original:[],vocals:[]});await Promise.resolve();
  assert.match(canvas.tools.children.at(-1).textContent,/精细波形/);
  canvas.disposeTimeline();assert.ok(requests[1].signal.aborted);
});
test('review selection does not scroll to segment cards',async()=>{
  const source=await readFile(new URL('../web/ty-h3lv.js',import.meta.url),'utf8');
  assert.ok(!source.includes('scrollIntoView'));
  assert.match(source,/createTimeline\(canvas, state,/);
});

test('range mode draws selection, drags handles and never edits existing cut points',()=>{
  let rects=0, boundaries=0, selection={start:2,end:6}, busy=false;
  const ctx=new Proxy({}, {get:(_,name)=>name==='measureText'?()=>({width:36}):
    name==='strokeRect'?()=>rects++:()=>{}});
  class Node {
    children=[]; value='0';
    append(n){this.children.push(n);} before(n){(this.beforeNodes??=[]).push(n);} after(){}
    setAttribute(){} addEventListener(name,fn){this[name]=fn;}
    getBoundingClientRect(){return {left:0,width:1000};} getContext(){return ctx;}
    setPointerCapture(){} hasPointerCapture(){return false;}
  }
  globalThis.document={createElement:()=>new Node()};globalThis.window={devicePixelRatio:1};
  globalThis.ResizeObserver=class{observe(){} disconnect(){}};
  const canvas=new Node(),plan={id:'clip',duration:10,max_seconds:10,segments:[{},{}]};
  const rows=[{end:{value:5}},{end:{value:10}}], changes=[];
  createTimeline(canvas,()=>({plan,rows,selected:0,clipSelection:selection,clipBusy:busy}),
    ()=>{},()=>boundaries++,()=>boundaries++,null,{
      activate(){},confirm(){},cancel(){selection=null;},
      change(range,edge){selection=range;changes.push(edge);},
    });
  canvas.drawTimeline(); assert.ok(rects>0);
  const evt=t=>({clientX:50+t/10*932,button:0,pointerId:1});
  canvas.onpointerdown(evt(2));canvas.onpointermove(evt(3));canvas.onpointerup(evt(3));
  assert.equal(selection.start,3);assert.equal(changes.at(-1),'start');
  canvas.onpointerdown(evt(4));canvas.onpointermove(evt(8));canvas.onpointerup(evt(8));
  assert.equal(selection.start,4);assert.equal(selection.end,8);assert.equal(boundaries,0);
  busy=true;canvas.onpointerdown(evt(4));canvas.onpointermove(evt(6));canvas.onpointerup(evt(6));
  assert.equal(selection.start,4);
  canvas.disposeTimeline();
});

test('cut toolbar, selection, context menu and Delete stay scoped to the focused timeline',async()=>{
  const ctx=new Proxy({}, {get:(_,name)=>name==='measureText'?()=>({width:36}):()=>{}});
  const elements=[];
  class Node {
    children=[]; style={}; value='0';
    append(n){this.children.push(n);} before(n){elements.push(n);} after(){}
    replaceChildren(...items){this.children=items;} contains(target){return this.children.includes(target);}
    setAttribute(){} addEventListener(){} focus(){} remove(){}
    getBoundingClientRect(){return {left:0,width:1000};} getContext(){return ctx;}
    setPointerCapture(){} hasPointerCapture(){return false;}
  }
  globalThis.document={createElement:()=>new Node(),addEventListener(){},removeEventListener(){}};
  globalThis.window={devicePixelRatio:1,innerWidth:1000,innerHeight:800};
  globalThis.ResizeObserver=class{observe(){} disconnect(){}};
  const canvas=new Node(),plan={id:'cuts',duration:10,max_seconds:10,segments:[{},{}]};
  const rows=[{end:{value:5}},{end:{value:10}}], events=[];
  let range=null,disabled=false;
  createTimeline(canvas,()=>({plan,rows,selected:0,previewTime:2,clipSelection:range}),
    ()=>{},()=>{},()=>{},null,null,{
      disabled:()=>disabled,split:t=>events.push(['split',t]),remove:i=>events.push(['remove',i]),error:e=>{throw e;},
    });
  canvas.drawTimeline();
  const toolbar=elements[0],menu=elements.find(item=>item.className==='ty-h3lv-cut-menu');
  toolbar.children.find(item=>item.textContent==='在播放头切割').onclick();
  await Promise.resolve();
  assert.deepEqual(events.pop(),['split',2]);
  const pointer={clientX:516,clientY:200,button:0,pointerId:1};
  canvas.onpointerdown(pointer);canvas.onpointerup(pointer);
  let prevented=0;
  const key={key:'Delete',target:canvas,preventDefault(){prevented++;},stopPropagation(){}};
  canvas.onkeydown({...key,target:{}});
  assert.equal(events.length,0);
  canvas.onkeydown(key);await Promise.resolve();
  assert.deepEqual(events.pop(),['remove',0]);assert.equal(prevented,1);
  canvas.oncontextmenu({...pointer,preventDefault(){},stopPropagation(){}});
  assert.equal(menu.hidden,false);
  assert.match(menu.children[0].textContent,/删除切点/);
  await menu.children[0].onclick();
  assert.deepEqual(events.pop(),['remove',0]);
  canvas.oncontextmenu({...pointer,clientX:50+3/10*932,preventDefault(){},stopPropagation(){}});
  await menu.children[0].onclick();
  assert.deepEqual(events.pop(),['split',3]);
  disabled=true;canvas.drawTimeline();
  assert.equal(toolbar.children.find(item=>item.textContent==='在播放头切割').disabled,true);
  range={start:1,end:3};disabled=false;canvas.drawTimeline();
  assert.equal(toolbar.children.find(item=>item.textContent==='在播放头切割').disabled,true);
  canvas.disposeTimeline();
});

test('source clipping shows one audio track and distinguishes a click from range dragging',()=>{
  const labels=[],nodes=[];
  const ctx=new Proxy({}, {get:(_,name)=>name==='measureText'?()=>({width:36}):
    name==='fillText'?text=>labels.push(text):()=>{}});
  class Node {
    children=[]; value='0';
    append(n){this.children.push(n);} before(n){nodes.push(n);} after(n){nodes.push(n);}
    setAttribute(){} addEventListener(){} getBoundingClientRect(){return {left:0,width:1000};}
    getContext(){return ctx;} setPointerCapture(){} hasPointerCapture(){return false;}
  }
  globalThis.document={createElement:()=>new Node()};globalThis.window={devicePixelRatio:1};
  globalThis.ResizeObserver=class{observe(){} disconnect(){}};
  const canvas=new Node(),plan={id:'source',duration:40,max_seconds:40,segments:[{}]};
  const rows=[{end:{value:40}}];let selection={start:0,end:40},time=0;
  createTimeline(canvas,()=>({plan,rows,selected:0,analysis:{waveform:{original:[.5]}},
    clipSelection:selection,previewTime:time}),(_index,value)=>{time=value;},
    ()=>assert.fail('edited cuts'),()=>assert.fail('committed cuts'),null,{
      only:true,change:range=>{selection=range;},cancel(){},confirm(){},
    });
  canvas.drawTimeline();
  assert.equal(nodes[0].hidden,true);
  assert.ok(labels.includes('原音轨'));
  assert.ok(!labels.includes('人声'));
  assert.ok(!labels.some(text=>text.startsWith('第')));
  const evt=t=>({clientX:50+t/40*932,button:0,pointerId:1});
  canvas.onpointerdown(evt(10));canvas.onpointerup(evt(10));
  assert.equal(time,10);assert.deepEqual(selection,{start:0,end:40});
  canvas.onpointerdown(evt(5));canvas.onpointermove(evt(30));canvas.onpointerup(evt(30));
  assert.deepEqual(selection,{start:5,end:30});
  canvas.onpointerdown(evt(5));canvas.onpointermove(evt(6));canvas.onpointerup(evt(6));
  assert.equal(selection.start,6);
  const inputs=nodes.find(node=>node.className==='ty-h3lv-clip-controls');
  assert.equal(inputs.hidden,false);
  assert.equal(inputs.children.at(-2).textContent,'取消');
  assert.equal(inputs.children.at(-1).textContent,'确认截取');
  canvas.disposeTimeline();
});
