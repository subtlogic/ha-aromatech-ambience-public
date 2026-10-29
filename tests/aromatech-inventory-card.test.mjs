import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const source=await fs.readFile(new URL('../examples/www/aromatech-inventory-card.js',import.meta.url),'utf8');
const {statusView,choices,buildPayload,AromaTechInventoryCard}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const inv={active_bottle_id:'active',scents:{s:{id:'s',name:'Shared'},unused:{id:'unused',name:'Shared'}},bottles:{
 active:{id:'active',name:'Bottle',empty_g:20,fills:[{scent_id:'s'}]},
 stored:{id:'stored',name:'Bottle',empty_g:20,fills:[{scent_id:'s'}]},
 archived:{id:'archived',name:'Old',empty_g:30,archived:true,fills:[{scent_id:'s'}]},
}};
test('status colours retain text meaning and unknown never becomes green',()=>{
 for(const [state,color] of [['ok','green'],['low','red'],['estimated_depleted','red'],['refill_soon','yellow'],['needs_weighing','yellow'],['needs_calibration','yellow'],['no_bottle','gray'],['forecast_unavailable','gray'],['unknown','gray'],['unavailable','gray']]){
  const view=statusView({states:{status:{state},percent:{state:'unknown'},power:{state:'off'}}},{status:'status',percent:'percent',power:'power'});
  assert.equal(view.color,color);assert.equal(view.power,'Off');assert.equal(view.percent,'Remaining unknown');assert.ok(view.label);
 }
});
test('known zero is displayed; missing readings are not zero',()=>{
 const entities={status:'status',percent:'percent',intensity:'intensity',scent:'scent',power:'power'};
 const view=statusView({states:Object.fromEntries(Object.entries({status:'estimated_depleted',percent:'0',intensity:'2',scent:'<script>alert(1)</script>',power:'on'}).map(([k,state])=>[k,{state}]))},entities);
 assert.equal(view.percent,'~0% remaining');assert.equal(view.intensity,'2');assert.equal(view.power,'On');assert.equal(view.scent,'<script>alert(1)</script>');
 assert.equal(statusView({},entities).intensity,'Unknown');
});
test('choices disambiguate names and prevent invalid lifecycle transitions',()=>{
 assert.deepEqual(choices(inv,'install_bottle','bottles').map(x=>x.value),['stored']);
 assert.deepEqual(choices(inv,'store_bottle','bottles').map(x=>x.value),['active']);
 assert.deepEqual(choices(inv,'delete_bottle','bottles').map(x=>x.value),['stored','archived']);
 assert.deepEqual(choices(inv,'delete_scent','scents').map(x=>x.value),['unused']);
 assert.match(choices(inv,'edit_scent','scents')[1].label,/unused/);
});
test('creation sends only backend-supported fields and both rate values',()=>{
 assert.deepEqual(buildPayload('add_scent',{name:' Hotel ',brand:'VINEVIDA',notes:'note',grams_per_spray:'.008',spray_seconds:'10'},inv,'on'),{action:'add_scent',name:'Hotel',brand:'VINEVIDA',notes:'note',grams_per_spray:.008,spray_seconds:10});
 assert.deepEqual(buildPayload('add_bottle',{name:'A',notes:'',scent_id:'s',empty_g:'20',gross_g:'120'},inv,'on'),{action:'add_bottle',scent_id:'s',name:'A',notes:'',empty_g:20,gross_g:120});
});
test('blank, negative, boolean, infinite and oversized quantities are rejected',()=>{
 for(const bad of ['',null,undefined,-1,NaN,Infinity,'Infinity',true,10001])assert.throws(()=>buildPayload('record_weight',{bottle_id:'active',gross_g:bad},inv,'off'));
 assert.throws(()=>buildPayload('record_weight',{bottle_id:'active',gross_g:19},inv,'off'),/below/);
 assert.throws(()=>buildPayload('add_scent',{name:'S',grams_per_spray:'.008'},inv,'on'),/required/);
 assert.throws(()=>buildPayload('add_scent',{name:'S',grams_per_spray:0,spray_seconds:10},inv,'on'));
});
test('metadata preserves identities and forbids unrecognized input leakage',()=>{
 const p=buildPayload('edit_bottle',{bottle_id:'stored',name:'Renamed',notes:'saved',empty_g:20,gross_g:999,confirm:true},inv,'on');
 assert.deepEqual(p,{action:'edit_bottle',bottle_id:'stored',name:'Renamed',notes:'saved',empty_g:20});
 assert.throws(()=>buildPayload('edit_scent',{scent_id:'missing',name:'A'},inv,'on'),/eligible/);
 assert.throws(()=>buildPayload('add_scent',{name:'x'.repeat(101)},inv,'on'),/100/);
});
test('delete requires explicit confirmation and never deletes active or referenced records',()=>{
 assert.throws(()=>buildPayload('delete_bottle',{bottle_id:'stored'},inv,'off'),/Confirm/);
 assert.throws(()=>buildPayload('delete_bottle',{bottle_id:'active',confirm:true},inv,'off'),/eligible/);
 assert.throws(()=>buildPayload('delete_scent',{scent_id:'s',confirm:true},inv,'off'),/eligible/);
 assert.deepEqual(buildPayload('delete_scent',{scent_id:'unused',confirm:true},inv,'on'),{action:'delete_scent',scent_id:'unused',confirm:true});
});
test('installation and storage require observed off and valid bottle state',()=>{
 for(const power of ['on','unknown','unavailable'])assert.throws(()=>buildPayload('install_bottle',{bottle_id:'stored'},inv,power),/off/);
 assert.throws(()=>buildPayload('install_bottle',{bottle_id:'archived'},inv,'off'),/eligible/);
 assert.deepEqual(buildPayload('install_bottle',{bottle_id:'stored'},inv,'off'),{action:'install_bottle',bottle_id:'stored'});
 assert.deepEqual(buildPayload('store_bottle',{bottle_id:'active'},inv,'off'),{action:'store_bottle',bottle_id:'active'});
});
test('refill, corrections, archive and restore retain exact action contracts',()=>{
 assert.deepEqual(buildPayload('refill_bottle',{bottle_id:'stored',scent_id:'unused',gross_g:100},inv,'on'),{action:'refill_bottle',scent_id:'unused',bottle_id:'stored',gross_g:100});
 assert.throws(()=>buildPayload('refill_bottle',{bottle_id:'active',scent_id:'unused',gross_g:100},inv,'off'),/eligible/);
 assert.throws(()=>buildPayload('correct_weight',{bottle_id:'active',gross_g:100},inv,'off'),/reason/);
 assert.deepEqual(buildPayload('correct_weight',{bottle_id:'active',gross_g:100,reason:'Scale correction'},inv,'on'),{action:'correct_weight',bottle_id:'active',gross_g:100,reason:'Scale correction'});
 assert.equal(buildPayload('archive_bottle',{bottle_id:'archived',archived:false},inv,'on').archived,false);
});
function harness(service){
 return {_busy:false,_data:{inventory:inv},_action:'edit_bottle',_error:{textContent:''},_success:{textContent:''},config:{entities:{power:'power'}},_hass:{states:{power:{state:'on'}}},
  _fields:()=>({bottle_id:'stored',name:'Changed',empty_g:20,notes:''}),_setBusy(v){this._busy=v;},_service:service,_read:async()=>{},_renderManager(){this.rendered=true;}};
}
test('duplicate submissions are suppressed while save is pending',async()=>{
 let resolve,calls=0;const h=harness(()=>{calls++;return new Promise(r=>resolve=r);});
 const first=AromaTechInventoryCard.prototype._submit.call(h);
 await AromaTechInventoryCard.prototype._submit.call(h);assert.equal(calls,1);assert.equal(h._busy,true);
 resolve();await first;assert.equal(h._busy,false);assert.match(h._success.textContent,/saved/);
});
test('service failure keeps form and exposes error without pretending save succeeded',async()=>{
 const h=harness(async()=>{throw Error('Store failed');});
 await AromaTechInventoryCard.prototype._submit.call(h);
 assert.match(h._error.textContent,/Not saved: Store failed/);assert.equal(h.rendered,undefined);assert.equal(h._busy,false);
});
test('save followed by failed refresh disables stale inventory',async()=>{
 const h=harness(async()=>{});h._read=async()=>{throw Error('Connection lost');};
 await AromaTechInventoryCard.prototype._submit.call(h);
 assert.match(h._error.textContent,/Saved, but refresh failed/);assert.equal(h._data,null);
});
test('HA state setter updates status without rerendering draft form or saving',()=>{
 let updates=0,loads=0;const h={config:{},_update(){updates++;},_loadOnce(){loads++;},_controls:{name:{value:'Unfinished edit'}}};
 Object.getOwnPropertyDescriptor(AromaTechInventoryCard.prototype,'hass').set.call(h,{states:{}});
 assert.equal(h._controls.name.value,'Unfinished edit');assert.equal(updates,1);assert.equal(loads,1);
});
test('read uses response-only service and unwraps the HA response envelope',async()=>{
 let request;const h={config:{entry_id:'entry'},_inventoryLine:{textContent:''},_hass:{callWS:async r=>{request=r;return {response:{inventory:inv,storage_error:null}};}}};
 await AromaTechInventoryCard.prototype._read.call(h);
 assert.equal(request.return_response,true);assert.equal(request.service,'list_inventory');assert.equal(request.service_data.config_entry_id,'entry');assert.equal(h._data.inventory,inv);
});
test('changing delete selection clears consent and identifies the selected record',()=>{
 const h={_controls:{confirm:{checked:true}},_deleteWarning:{textContent:''},_action:'delete_bottle'};
 AromaTechInventoryCard.prototype._recordChanged.call(h,{id:'bottle-abcdefgh',name:'Bottle B'});
 assert.equal(h._controls.confirm.checked,false);assert.match(h._deleteWarning.textContent,/Bottle B/);assert.match(h._deleteWarning.textContent,/abcdefgh/);assert.match(h._deleteWarning.textContent,/all its fill/);
 h._controls.confirm.checked=true;
 AromaTechInventoryCard.prototype._recordChanged.call(h,{id:'bottle-ijklmnop',name:'Bottle C'});
 assert.equal(h._controls.confirm.checked,false);assert.match(h._deleteWarning.textContent,/Bottle C/);assert.doesNotMatch(h._deleteWarning.textContent,/Bottle B/);
});
test('learned rate is visible and replacing it requires specific consent',()=>{
 const learned=structuredClone(inv);learned.scents.s.rate_source='learned';learned.scents.s.rate_g_per_second=.0008;
 const fields={scent_id:'s',name:'Shared',grams_per_spray:'.01',spray_seconds:'10'};
 assert.throws(()=>buildPayload('edit_scent',fields,learned,'on'),/Confirm replacement/);
 const payload=buildPayload('edit_scent',{...fields,replace_learned_rate:true},learned,'on');
 assert.equal(payload.grams_per_spray,.01);assert.equal(Object.hasOwn(payload,'replace_learned_rate'),false);
 const rename=buildPayload('edit_scent',{scent_id:'s',name:'New name'},learned,'on');assert.equal(Object.hasOwn(rename,'grams_per_spray'),false);
 const h={_controls:{replace_learned_rate:{checked:true}},_rateInfo:{textContent:''},_replaceRate:{hidden:true}};
 AromaTechInventoryCard.prototype._recordChanged.call(h,learned.scents.s);
 assert.equal(h._controls.replace_learned_rate.checked,false);assert.equal(h._replaceRate.hidden,false);assert.match(h._rateInfo.textContent,/learned/);assert.match(h._rateInfo.textContent,/0.00080000/);
});
test('no bottle never displays a legacy oil mass',()=>{
 const hass={states:{status:{state:'no_bottle'},mass:{state:'52.31'}}};
 assert.equal(statusView(hass,{status:'status',mass:'mass'}).mass,null);
 hass.states.status.state='ok';assert.equal(statusView(hass,{status:'status',mass:'mass'}).mass,'52.31');
});
test('management reopening refreshes once and retains unsaved drafts',()=>{
 const previous=globalThis.window;globalThis.window={location:{hash:'#aromatech'}};
 try{
  let refreshes=0;const h={config:{mode:'manage'},_loaded:true,_hass:{},_busy:false,_dirty:false,_success:{textContent:''},_refresh(){refreshes++;}};
  AromaTechInventoryCard.prototype._onNavigation.call(h);
  window.location.hash='#aromatech-manage';AromaTechInventoryCard.prototype._onNavigation.call(h);AromaTechInventoryCard.prototype._onNavigation.call(h);
  assert.equal(refreshes,1);
  window.location.hash='#aromatech';AromaTechInventoryCard.prototype._onNavigation.call(h);h._dirty=true;
  window.location.hash='#aromatech-manage';AromaTechInventoryCard.prototype._onNavigation.call(h);
  assert.equal(refreshes,1);assert.match(h._success.textContent,/Unsaved changes retained/);
  h._manageWasOpen=false;h._dirty=false;h._busy=true;AromaTechInventoryCard.prototype._onNavigation.call(h);assert.equal(refreshes,1);
 }finally{globalThis.window=previous;}
});
test('navigation listeners are removed on disconnect and never duplicated',()=>{
 const previous=globalThis.window;const registered=new Map();let adds=0,removes=0;
 globalThis.window={addEventListener(name,fn){registered.set(name,fn);adds++;},removeEventListener(name,fn){assert.equal(registered.get(name),fn);registered.delete(name);removes++;}};
 try{
  const h={_navigationListener:()=>{},_onNavigation(){},_loadOnce(){}};
  AromaTechInventoryCard.prototype.connectedCallback.call(h);AromaTechInventoryCard.prototype.connectedCallback.call(h);assert.equal(adds,3);
  AromaTechInventoryCard.prototype.disconnectedCallback.call(h);assert.equal(removes,3);assert.equal(registered.size,0);
  AromaTechInventoryCard.prototype.connectedCallback.call(h);assert.equal(adds,6);
  AromaTechInventoryCard.prototype.disconnectedCallback.call(h);assert.equal(registered.size,0);
 }finally{globalThis.window=previous;}
});

test('name-only scent leaves rate selection to the integration default',()=>{
 assert.deepEqual(buildPayload('add_scent',{name:' Cedar '},inv,'on'),{action:'add_scent',name:'Cedar',brand:'',notes:''});
 assert.equal(Object.hasOwn(buildPayload('edit_scent',{scent_id:'s',name:'Renamed'},inv,'on'),'grams_per_spray'),false);
});
test('nominal starting amounts default to 60 without inventing empty-bottle weight',()=>{
 const fields={name:'New bottle',scent_id:'s'};
 assert.deepEqual(buildPayload('add_bottle',fields,inv,'on'),{action:'add_bottle',scent_id:'s',name:'New bottle',notes:'',oil_g:60});
 for(const size of ['30','60','120']){
  const payload=buildPayload('add_bottle',{...fields,starting_amount:size,empty_g:'999',gross_g:'invalid',oil_g:'invalid'},inv,'on');
  assert.equal(payload.oil_g,Number(size));assert.equal(Object.hasOwn(payload,'empty_g'),false);assert.equal(Object.hasOwn(payload,'gross_g'),false);assert.equal(Object.hasOwn(payload,'starting_amount'),false);
 }
 assert.throws(()=>buildPayload('add_bottle',{...fields,starting_amount:'90'},inv,'on'),/Choose/);
});
test('custom amounts and measured amounts remain distinct and reject conflicting input',()=>{
 const fields={name:'New',scent_id:'s'};
 assert.equal(buildPayload('add_bottle',{...fields,starting_amount:'custom',oil_g:'42.5',gross_g:'bad'},inv,'on').oil_g,42.5);
 for(const oil_g of ['',0,-1,Infinity,true])assert.throws(()=>buildPayload('add_bottle',{...fields,starting_amount:'custom',oil_g},inv,'on'));
 assert.throws(()=>buildPayload('add_bottle',{...fields,oil_g:60,gross_g:100,empty_g:20},inv,'on'),/not both/);
 const p=buildPayload('add_bottle',{...fields,starting_amount:'measured',gross_g:'70',empty_g:'20',oil_g:60},inv,'on');
 assert.equal(p.gross_g,70);assert.equal(p.empty_g,20);assert.equal(Object.hasOwn(p,'oil_g'),false);
});
test('unknown tare blocks gross weights while known zero and preset refills remain usable',()=>{
 const unknown=structuredClone(inv);unknown.bottles.stored.empty_g=null;
 for(const action of ['record_weight','correct_weight','refill_bottle'])assert.throws(()=>buildPayload(action,{bottle_id:'stored',scent_id:'s',gross_g:60,reason:'Check'},unknown,'off'),/empty bottle weight first/);
 assert.equal(buildPayload('refill_bottle',{bottle_id:'stored',scent_id:'s',starting_amount:'120'},unknown,'off').oil_g,120);
 assert.equal(buildPayload('edit_bottle',{bottle_id:'stored',name:'Renamed',empty_g:''},unknown,'on').empty_g,undefined);
 unknown.bottles.stored.empty_g=0;
 assert.equal(buildPayload('record_weight',{bottle_id:'stored',gross_g:60},unknown,'off').gross_g,60);
 assert.throws(()=>buildPayload('add_bottle',{name:'New',scent_id:'s',starting_amount:'measured',gross_g:60},inv,'on'),/empty bottle weight first/);
});
