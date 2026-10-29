/* AromaTech inventory UI. Uses integration services; owns no inventory storage. */
const DOMAIN = 'aromatech_ambience';
export const STATUS = Object.freeze({
  ok: ['OK', 'green'], refill_soon: ['Refill soon', 'yellow'],
  needs_weighing: ['Needs weighing', 'yellow'], needs_calibration: ['Needs calibration', 'yellow'],
  low: ['Low oil', 'red'], estimated_depleted: ['Estimated depleted', 'red'],
  no_bottle: ['No bottle installed', 'gray'], forecast_unavailable: ['Forecast unavailable', 'gray'],
});
export const ACTIONS = Object.freeze({
  add_scent: 'Add scent', edit_scent: 'Edit scent', delete_scent: 'Delete scent',
  add_bottle: 'Add bottle', edit_bottle: 'Edit bottle', install_bottle: 'Install / swap bottle',
  store_bottle: 'Store installed bottle', record_weight: 'Record weighing',
  correct_weight: 'Correct balance', refill_bottle: 'Refill stored bottle',
  archive_bottle: 'Archive / restore bottle', delete_bottle: 'Delete bottle',
});
const UNKNOWN = new Set(['unknown', 'unavailable', '', null, undefined]);
const value = (hass, entities, key) => hass?.states?.[entities?.[key]]?.state;
const numeric = v => !UNKNOWN.has(v) && Number.isFinite(Number(v)) ? Number(v) : null;
const numberText = (v, unit = '', digits = 1) => numeric(v) === null ? 'Unknown' : `${Number(v).toFixed(digits)}${unit}`;
export function statusView(hass, entities = {}) {
  const raw = value(hass, entities, 'status');
  const [label, color] = STATUS[raw] || ['Unavailable', 'gray'];
  const power = value(hass, entities, 'power');
  return { label, color, raw, scent: UNKNOWN.has(value(hass, entities, 'scent')) ? 'No scent selected' : value(hass, entities, 'scent'),
    intensity: numberText(value(hass, entities, 'intensity'), '', 0),
    percent: numeric(value(hass, entities, 'percent')) === null ? 'Remaining unknown' : `~${numberText(value(hass, entities, 'percent'), '%', 0)} remaining`,
    mass: raw === 'no_bottle' ? null : value(hass, entities, 'mass'),
    power: power === 'on' ? 'On' : power === 'off' ? 'Off' : 'Unavailable' };
}
export function choices(inventory, action, kind) {
  const records = Object.values(inventory?.[kind] || {});
  return records.filter(record => {
    if (kind === 'scents') return action !== 'delete_scent' || !Object.values(inventory.bottles || {}).some(b => b.fills.some(f => f.scent_id === record.id));
    const active = record.id === inventory.active_bottle_id;
    if (action === 'store_bottle') return active;
    if (action === 'install_bottle') return !active && !record.archived;
    if (['refill_bottle', 'archive_bottle', 'delete_bottle'].includes(action)) return !active;
    return true;
  }).map(r => ({ value: r.id, label: `${r.name} · ${r.id.slice(-8)}${r.archived ? ' · Archived' : ''}` }));
}
function quantity(v, label, positive = false) {
  if (typeof v === 'boolean' || v === null || v === undefined || String(v).trim() === '') throw Error(`${label} is required.`);
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0 || n > 10000 || (positive && n === 0)) throw Error(`${label} must be ${positive ? 'greater than zero and ' : ''}between 0 and 10000.`);
  return n;
}
function text(v, label, required = false, max = 4096) {
  const s = String(v ?? '').trim();
  if ((required && !s) || s.length > max) throw Error(`${label} ${!s ? 'is required' : `must be at most ${max} characters`}.`);
  return s;
}
export function buildPayload(action, fields, inventory, power) {
  if (!Object.hasOwn(ACTIONS, action)) throw Error('Choose an inventory action.');
  const p = { action };
  const scentActions = ['edit_scent', 'delete_scent', 'add_bottle', 'refill_bottle'];
  const bottleAction = !['add_scent', 'edit_scent', 'delete_scent', 'add_bottle'].includes(action);
  for (const [needed, key, kind] of [[scentActions.includes(action), 'scent_id', 'scents'], [bottleAction, 'bottle_id', 'bottles']]) {
    if (needed) {
      if (!choices(inventory, action, kind).some(r => r.value === fields[key])) throw Error(`Choose an eligible ${kind === 'scents' ? 'scent' : 'bottle'}; refresh if it changed.`);
      p[key] = fields[key];
    }
  }
  if (['install_bottle', 'store_bottle'].includes(action) && power !== 'off') throw Error('Turn scenting off before changing the installed bottle.');
  if (['add_scent', 'edit_scent', 'add_bottle', 'edit_bottle'].includes(action)) {
    p.name = text(fields.name, 'Name', true, 100);
    p.notes = text(fields.notes, 'Notes');
  }
  if (['add_scent', 'edit_scent'].includes(action)) {
    p.brand = text(fields.brand, 'Brand');
    const hasG = String(fields.grams_per_spray ?? '').trim() !== '', hasS = String(fields.spray_seconds ?? '').trim() !== '';
    if (hasG || hasS) {
      if (action === 'edit_scent' && inventory.scents[p.scent_id].rate_source === 'learned' && fields.replace_learned_rate !== true) throw Error('Confirm replacement of the learned rate with this provisional default.');
      p.grams_per_spray = quantity(fields.grams_per_spray, 'Grams per spray', true);
      p.spray_seconds = quantity(fields.spray_seconds, 'Reference spray seconds', true);
    }
  }
  const entered = v => v !== null && v !== undefined && String(v).trim() !== '';
  const starting = ['add_bottle', 'refill_bottle'].includes(action);
  const mode = fields.starting_amount;
  if (mode !== undefined && !['30', '60', '120', 'custom', 'measured'].includes(mode)) throw Error('Choose a starting amount.');
  if (['add_bottle', 'edit_bottle'].includes(action) && entered(fields.empty_g) && (!starting || mode === undefined || mode === 'measured')) {
    p.empty_g = quantity(fields.empty_g, 'Empty bottle weight');
  }
  if (starting) {
    if (mode === 'custom') p.oil_g = quantity(fields.oil_g, 'Starting oil amount', true);
    else if (['30', '60', '120'].includes(mode)) p.oil_g = Number(mode);
    else if (mode === 'measured' || entered(fields.gross_g)) {
      if (mode === undefined && entered(fields.oil_g)) throw Error('Supply oil amount or gross weight, not both.');
      p.gross_g = quantity(fields.gross_g, 'Gross weight', true);
    } else p.oil_g = entered(fields.oil_g) ? quantity(fields.oil_g, 'Starting oil amount', true) : 60;
  } else if (['record_weight', 'correct_weight'].includes(action)) p.gross_g = quantity(fields.gross_g, 'Gross weight', true);
  if (p.gross_g !== undefined) {
    const tare = action === 'add_bottle' ? p.empty_g : inventory.bottles[p.bottle_id].empty_g;
    if (tare === null || tare === undefined) throw Error('Enter the empty bottle weight first using Edit bottle before recording a gross weight.');
    if (p.gross_g < tare) throw Error('Gross weight cannot be below the empty bottle weight.');
  }
  if (action === 'correct_weight') p.reason = text(fields.reason, 'Correction reason', true);
  if (action === 'archive_bottle') p.archived = fields.archived === true;
  if (action.startsWith('delete_')) {
    if (fields.confirm !== true) throw Error('Confirm permanent deletion before continuing.');
    p.confirm = true;
  }
  return p;
}

const CSS = `
:host{display:block;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,inherit)}[hidden]{display:none!important}
ha-card{display:block;overflow:hidden;border-radius:var(--ha-card-border-radius,16px);background:var(--ha-card-background,var(--card-background-color));padding:18px;box-sizing:border-box}
*{box-sizing:border-box}h2,h3,p{margin:0}h2{font-size:18px;font-weight:600}h3{font-size:16px}p{line-height:1.5}.muted,.hint{color:var(--secondary-text-color);font-size:13px}.hint{margin:10px 0}
.heading,.row,.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.heading{margin-bottom:10px}.title{display:flex;align-items:center;gap:9px}.status{display:inline-flex;gap:7px;align-items:center;font-size:13px;font-weight:600;padding:5px 9px;border-radius:20px;background:color-mix(in srgb,var(--status-color) 12%,transparent);color:var(--status-color)}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}.green{--status-color:#2e9b55}.yellow{--status-color:#a87900}.red{--status-color:#d44242}.gray{--status-color:var(--secondary-text-color,#777)}
.scent{font-size:17px;font-weight:600;overflow-wrap:anywhere;margin:8px 0 6px}.metrics{display:flex;gap:12px;flex-wrap:wrap;font-size:14px}.power{font-size:12px;color:var(--secondary-text-color);margin-top:9px}.summary-link{cursor:pointer}.summary-link:focus-visible{outline:2px solid var(--primary-color);outline-offset:-3px}.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:16px 0}.stat{padding:11px;border-radius:12px;background:var(--secondary-background-color)}.stat strong{display:block;margin-top:5px;font-size:18px}.stat span{font-size:12px;color:var(--secondary-text-color)}
button,input,select,textarea{font:inherit}button{border:1px solid var(--divider-color);border-radius:10px;min-height:44px;padding:9px 13px;background:var(--secondary-background-color);color:var(--primary-text-color);cursor:pointer}button.primary{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}button.danger{color:var(--error-color,#c62828)}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--primary-color);outline-offset:2px}
.toolbar{justify-content:flex-start;margin-top:14px}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:6px;font-size:13px;min-width:0}.full{grid-column:1/-1}input,select,textarea{width:100%;min-height:44px;border:1px solid var(--divider-color);border-radius:9px;padding:10px;color:var(--primary-text-color);background:var(--card-background-color);box-sizing:border-box}textarea{min-height:75px;resize:vertical}.check{display:flex;align-items:flex-start;gap:10px;line-height:1.5}.check input{width:20px;min-height:20px;flex-shrink:0;margin:0}.operation{margin:16px 0}.error{color:var(--error-color,#c62828);background:color-mix(in srgb,var(--error-color,#c62828) 8%,transparent);border-radius:8px;padding:10px;margin-top:12px;overflow-wrap:anywhere}.success{color:var(--success-color,#2e9b55);margin-top:12px;font-size:14px}.error:empty,.success:empty{display:none}fieldset{border:0;padding:0;margin:0;min-width:0}.guide{padding:12px;border:1px solid var(--divider-color);border-radius:12px;margin:14px 0}.guide p{font-size:13px}.inventory-line{font-size:13px;color:var(--secondary-text-color);margin:8px 0}details{margin-top:14px}summary{cursor:pointer;font-size:13px}pre{white-space:pre-wrap;font:inherit;font-size:12px;overflow-wrap:anywhere;color:var(--secondary-text-color)}
@media(max-width:420px){ha-card{padding:14px}.form-grid{grid-template-columns:1fr}.toolbar button{flex:1}.heading{align-items:flex-start}.scent{font-size:16px}}`;

function el(tag, textValue, attrs = {}) {
  const node = document.createElement(tag);
  if (textValue !== undefined && textValue !== null) node.textContent = textValue;
  for (const [key, val] of Object.entries(attrs)) node.setAttribute(key, String(val));
  return node;
}
function button(label, fn, className = '') {
  const b = el('button', label, { type: 'button', class: className }); b.addEventListener('click', fn); return b;
}
export class AromaTechInventoryCard extends (globalThis.HTMLElement || class {}) {
  constructor() { super(); this._busy = false; this._loaded = false; this._dirty = false; this._data = null; this._action = 'add_scent'; this._controls = {}; this._navigationListener = () => this._onNavigation(); this.attachShadow({ mode: 'open' }); }
  setConfig(config) {
    if (!config.entry_id || !['summary', 'detail', 'manage'].includes(config.mode || 'summary')) throw Error('AromaTech card requires entry_id and a valid mode.');
    this.config = { ...config, mode: config.mode || 'summary', entities: { ...config.entities } };
    this._loaded = false; this._dirty = false; this._data = null; this._build(); this._update(); this._loadOnce(); this._onNavigation();
  }
  set hass(hass) { this._hass = hass; if (!this.config) return; this._update(); this._loadOnce(); }
  get hass() { return this._hass; }
  getCardSize() { return this.config?.mode === 'manage' ? 9 : this.config?.mode === 'detail' ? 5 : 2; }
  connectedCallback() {
    if (!this._listening) {
      for (const name of ['hashchange', 'location-changed', 'popstate']) window.addEventListener(name, this._navigationListener);
      this._listening = true;
    }
    this._onNavigation(); this._loadOnce();
  }
  disconnectedCallback() {
    if (this._listening) for (const name of ['hashchange', 'location-changed', 'popstate']) window.removeEventListener(name, this._navigationListener);
    this._listening = false; this._manageWasOpen = false;
  }
  _onNavigation() {
    if (this.config?.mode !== 'manage') return;
    const open = window.location.hash === (this.config.management_path || '#aromatech-manage');
    if (open && !this._manageWasOpen && this._loaded && this._hass && !this._busy) {
      if (this._dirty) this._success.textContent = 'Unsaved changes retained. Save them or discard edits to refresh the inventory.';
      else this._refresh();
    }
    this._manageWasOpen = open;
  }
  _loadOnce() { if (this.config?.mode === 'manage' && this._hass && !this._loaded) { this._loaded = true; this._refresh(); } }
  _build() {
    this.shadowRoot.replaceChildren(); const style = el('style', CSS); this.shadowRoot.append(style);
    this._card = el('ha-card'); this.shadowRoot.append(this._card);
    this._summary = el('div'); this._card.append(this._summary);
    if (this.config.mode === 'summary') {
      this._card.className = 'summary-link'; this._card.setAttribute('role', 'button'); this._card.tabIndex = 0;
      this._card.setAttribute('aria-label', 'Open AromaTech');
      this._card.addEventListener('click', () => this._navigate(this.config.navigation_path || '#aromatech'));
      this._card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this._navigate(this.config.navigation_path || '#aromatech'); } });
    } else if (this.config.mode === 'detail') {
      this._details = el('div'); this._card.append(this._details);
      const bar = el('div', null, { class: 'toolbar' });
      bar.append(button('Manage scents & bottles', () => this._navigate(this.config.management_path || '#aromatech-manage'), 'primary'));
      this._card.append(bar);
    } else {
      this._toolbar = el('div', null, { class: 'toolbar' });
      this._toolbar.append(button('Refresh inventory', () => {
        if (this._dirty && !window.confirm('Discard your unsaved form edits and refresh the inventory?')) return;
        this._dirty = false; this._refresh();
      }), button('Acknowledge alert', () => this._acknowledge()));
      this._card.append(this._toolbar);
      this._inventoryLine = el('p', 'Loading inventory…', { class: 'inventory-line' }); this._card.append(this._inventoryLine);
      this._manager = el('div'); this._card.append(this._manager);
    }
    this._error = el('div', '', { class: 'error', role: 'alert', 'aria-live': 'assertive' });
    this._success = el('div', '', { class: 'success', role: 'status', 'aria-live': 'polite' });
    this._card.append(this._error, this._success);
  }
  _navigate(path) { if (typeof path !== 'string' || !path.startsWith('#')) return; history.pushState(null, '', path); window.dispatchEvent(new Event('location-changed')); window.dispatchEvent(new Event('hashchange')); }
  _update() {
    if (!this._summary) return;
    const view = statusView(this._hass, this.config.entities);
    this._summary.replaceChildren();
    const heading = el('div', null, { class: 'heading' }); const title = el('div', null, { class: 'title' });
    const icon = el('ha-icon', null, { icon: 'mdi:scent' }); title.append(icon, el('h2', this.config.mode === 'manage' ? 'Scents & bottles' : 'AromaTech'));
    const badge = el('span', null, { class: `status ${view.color}` }); badge.append(el('span', '', { class: 'dot', 'aria-hidden': 'true' }), document.createTextNode(view.label));
    heading.append(title, badge);this._summary.append(heading, el('p', view.scent, { class: 'scent' }));
    const metrics = el('div', null, { class: 'metrics' });metrics.append(el('span', `Level ${view.intensity}`),el('span', view.percent));
    this._summary.append(metrics, el('p', `Scenting ${view.power} · oil status is estimated`, { class: 'power' }));
    if (this._details) {
      this._details.replaceChildren();const stats=el('div',null,{class:'stats'});
      for(const [label,key,unit,digits] of [['Oil remaining','mass',' g',2],['Days remaining','days',' days',1],['This fill','sprays',' sprays',1]]) {
        const stat=el('div',null,{class:'stat'});stat.append(el('span',label),el('strong',numberText(key==='mass'?view.mass:value(this._hass,this.config.entities,key),unit,digits)));stats.append(stat);
      }
      this._details.append(stats);
      const attrs=this._hass?.states?.[this.config.entities.mass]?.attributes||{};
      const lo=numeric(attrs.remaining_lower_g),hi=numeric(attrs.remaining_upper_g);
      if(view.raw!=='no_bottle'&&lo!==null&&hi!==null&&lo!==hi)this._details.append(el('p',`Estimated range: ${lo.toFixed(2)}–${hi.toFixed(2)} g. Weigh to reduce uncertainty.`,{class:'hint'}));
      this._details.append(el('p','Sprays are schedule-based estimates. Days remaining assumes the current recurring schedule continues.',{class:'hint'}));
    }
    this._syncPower();
  }
  _syncPower() {
    const power=value(this._hass,this.config.entities,'power');
    if(this._stopButton)this._stopButton.disabled=this._busy||power!=='on';
    if(this._resumeButton)this._resumeButton.disabled=this._busy||power!=='off';
    if(this._powerText)this._powerText.textContent=`Scenting is ${power==='on'?'On':power==='off'?'Off':'unavailable'}.`;
  }
  _setBusy(busy) {
    this._busy=busy;
    if(this._fieldset)this._fieldset.disabled=busy||!this._data||!!this._data.storage_error;
    this._toolbar?.querySelectorAll('button').forEach(b=>b.disabled=busy);
    this._syncPower();
  }
  async _read() {
    const result=await this._hass.callWS({type:'call_service',domain:DOMAIN,service:'list_inventory',service_data:{config_entry_id:this.config.entry_id},return_response:true});
    const data=result?.response;
    if(!data?.inventory?.scents||!data?.inventory?.bottles)throw Error('Inventory response is unavailable. Check the installed integration version.');
    this._data=data;
    this._inventoryLine.textContent=`${Object.keys(data.inventory.scents).length} scents · ${Object.keys(data.inventory.bottles).length} bottles · refreshed ${new Date().toLocaleTimeString()}`;
    if(data.storage_error)throw Error(`Inventory needs attention: ${data.storage_error}. Changes are disabled.`);
  }
  async _refresh() {
    if(this._busy||this._dirty)return;this._setBusy(true);this._error.textContent='';
    try{await this._read();this._renderManager();}catch(e){this._data=null;this._error.textContent=e.message||String(e);this._inventoryLine.textContent='Inventory unavailable. Refresh to retry.';}
    finally{this._setBusy(false);}
  }
  _renderManager() {
    this._manager.replaceChildren();this._controls={};this._fieldset=el('fieldset');this._manager.append(this._fieldset);
    const operation=el('label',null,{class:'field operation'});operation.append(el('span','What would you like to do?'));
    const select=el('select',null,{'aria-label':'Inventory action'});
    for(const [key,label] of Object.entries(ACTIONS)){const o=el('option',label,{value:key});select.append(o);}select.value=this._action;
    select.addEventListener('change',()=>{this._action=select.value;this._dirty=false;this._error.textContent='';this._success.textContent='';this._renderForm();});
    operation.append(select);this._fieldset.append(operation);this._formHolder=el('div');this._fieldset.append(this._formHolder);this._renderForm();
  }
  _field(parent,key,label,type='text',initial='',required=false) {
    const wrap=el('label',null,{class:`field ${type==='textarea'?'full':''}`});wrap.append(el('span',label));
    const input=el(type==='textarea'?'textarea':'input',null,{'aria-label':label,name:key});
    if(type!=='textarea')input.type=type;
    input.value=initial??'';input.required=required;
    if(type==='number'){input.min='0';input.max='10000';input.step='any';}
    if(type==='text'||type==='textarea')input.maxLength=key==='name'?100:4096;
    wrap.append(input);parent.append(wrap);this._controls[key]=input;return input;
  }
  _select(parent,key,label,options,onchange) {
    const wrap=el('label',null,{class:'field full'});wrap.append(el('span',label));
    const input=el('select',null,{'aria-label':label,name:key});input.required=true;
    if(!options.length)input.append(el('option','No eligible records',{value:''}));
    for(const option of options)input.append(el('option',option.label,{value:option.value}));
    input.addEventListener('change',()=>onchange?.(input.value));wrap.append(input);parent.append(wrap);this._controls[key]=input;return input;
  }
  _renderForm() {
    this._formHolder.replaceChildren();this._controls={};this._stopButton=null;this._resumeButton=null;this._deleteWarning=null;this._rateInfo=null;this._replaceRate=null;this._tareInfo=null;
    const action=this._action,inv=this._data.inventory;
    const form=el('form');const grid=el('div',null,{class:'form-grid'});form.append(grid);this._formHolder.append(form);
    form.addEventListener('input',()=>{this._dirty=true;});form.addEventListener('change',()=>{this._dirty=true;});
    const fillDefaults=()=>{
      const record=action.endsWith('scent')?inv.scents[this._controls.scent_id?.value]:inv.bottles[this._controls.bottle_id?.value];
      if(!record)return;
      this._recordChanged(record);
      if(action==='edit_scent'||action==='edit_bottle')for(const key of ['name','brand','notes','empty_g','grams_per_spray','spray_seconds'])if(this._controls[key])this._controls[key].value=record[key]??'';
      if(action==='archive_bottle'&&this._controls.archived)this._controls.archived.checked=!record.archived;
    };
    if(['edit_scent','delete_scent','add_bottle','refill_bottle'].includes(action))this._select(grid,'scent_id','Scent',choices(inv,action,'scents'),fillDefaults);
    if(!['add_scent','edit_scent','delete_scent','add_bottle'].includes(action))this._select(grid,'bottle_id','Bottle',choices(inv,action,'bottles'),fillDefaults);
    if(['add_scent','edit_scent','add_bottle','edit_bottle'].includes(action)){
      this._field(grid,'name','Name','text','',true);
      if(action!=='add_scent'){
        if(action.endsWith('scent'))this._field(grid,'brand','Brand');
        this._field(grid,'notes','Notes','textarea');
      }
    }
    if(['add_scent','edit_scent'].includes(action)){
      const details=el('details');details.append(el('summary','Optional scent details and consumption rate'));
      const advanced=el('div',null,{class:'form-grid'});details.append(advanced);form.append(details);
      if(action==='add_scent'){this._field(advanced,'brand','Brand');this._field(advanced,'notes','Notes','textarea');}
      this._field(advanced,'grams_per_spray','Grams per spray (optional)','number');this._field(advanced,'spray_seconds','Reference spray seconds (optional)','number');
      form.append(el('p',action==='add_scent'?'Uses an estimated 4 mg per 5 seconds of spraying. Add your own rate above or refine it later by weighing.':'Enter both rate fields to change the default, or leave both blank to keep it. Existing fills keep their own rates.',{class:'hint'}));
      if(action==='edit_scent'){
        this._rateInfo=el('p','',{class:'hint'});form.append(this._rateInfo);
        const wrap=el('label',null,{class:'check'});const check=el('input',null,{type:'checkbox',name:'replace_learned_rate'});
        wrap.append(check,el('span','Replace this learned consumption rate with the provisional rate entered above. Existing fills keep their own rates.'));
        this._controls.replace_learned_rate=check;this._replaceRate=wrap;form.append(wrap);
      }
    }
    if(['add_bottle','refill_bottle'].includes(action)){
      const amountFields=el('div',null,{class:'form-grid full'});
      const updateAmount=()=>{
        const mode=this._controls.starting_amount.value;
        for(const key of ['oil_g','empty_g','gross_g']){
          const input=this._controls[key];if(!input)continue;
          const show=key==='oil_g'?mode==='custom':mode==='measured';
          input.parentElement.hidden=!show;input.disabled=!show;input.required=show;
        }
      };
      const choice=this._select(grid,'starting_amount','Starting amount',[
        {value:'30',label:'30 bottle size'}, {value:'60',label:'60 bottle size'},
        {value:'120',label:'120 bottle size'}, {value:'custom',label:'Custom oil amount'},
        {value:'measured',label:'Measured bottle weight'},
      ],updateAmount);
      choice.value='60';
      this._field(amountFields,'oil_g','Starting oil amount (g)','number');
      if(action==='add_bottle')this._field(amountFields,'empty_g','Empty bottle weight (g)','number');
      this._field(amountFields,'gross_g','Gross weight, bottle + oil (g)','number');
      grid.append(amountFields);updateAmount();
      form.append(el('p','Approximate starting amount; weighing improves accuracy. Bottle sizes use the same number of grams as a simple estimate.',{class:'hint'}));
    }
    if(action==='edit_bottle')this._field(grid,'empty_g','Empty bottle weight (g, optional)','number');
    if(['record_weight','correct_weight'].includes(action))this._field(grid,'gross_g','Gross weight, bottle + oil (g)','number','',true);
    if(['refill_bottle','record_weight','correct_weight'].includes(action)){this._tareInfo=el('p','',{class:'hint'});form.append(this._tareInfo);}
    if(action==='correct_weight'){this._field(grid,'reason','Correction reason','textarea','',true);form.append(el('p','A correction preserves the old measurement and does not calibrate the consumption rate.',{class:'hint'}));}
    if(action==='edit_bottle')form.append(el('p','Changing the empty bottle weight requires a fresh weighing to restore the remaining-oil estimate.',{class:'hint'}));
    if(action==='refill_bottle')form.append(el('p','Creates a new fill for the same physical bottle and keeps earlier fill history. Store the bottle before refilling.',{class:'hint'}));
    if(action==='archive_bottle'){
      const wrap=el('label',null,{class:'check full'});const check=el('input',null,{type:'checkbox',name:'archived'});check.checked=true;wrap.append(check,el('span','Archive bottle (uncheck to restore it)'));grid.append(wrap);this._controls.archived=check;
    }
    if(action.startsWith('delete_')){
      const wrap=el('label',null,{class:'check full'});const check=el('input',null,{type:'checkbox',name:'confirm'});check.required=true;
      this._deleteWarning=el('span');wrap.append(check,this._deleteWarning);grid.append(wrap);this._controls.confirm=check;
      if(action==='delete_scent')form.append(el('p','Scents referenced by bottle history cannot be deleted. Only unused scents appear here.',{class:'hint'}));
    }
    fillDefaults();
    if(['install_bottle','store_bottle'].includes(action)){
      const guide=el('div',null,{class:'guide'});
      guide.append(el('h3','Change the physical bottle'),el('p','1. Stop scenting. 2. Change or remove the bottle. 3. Save your selection below. 4. Resume scenting when ready.'));
      this._powerText=el('p',null,{class:'hint'});guide.append(this._powerText);
      const bar=el('div',null,{class:'toolbar'});this._stopButton=button('Stop scenting',()=>this._power(false));this._resumeButton=button('Resume scenting',()=>this._power(true));bar.append(this._stopButton,this._resumeButton);guide.append(bar);form.append(guide);
    }
    const bar=el('div',null,{class:'toolbar'});const submit=el('button',ACTIONS[action],{type:'submit',class:action.startsWith('delete_')?'danger':'primary'});bar.append(submit);form.append(bar);
    form.addEventListener('submit',e=>{e.preventDefault();this._submit();});this._syncPower();
  }
  _recordChanged(record) {
    if(this._controls.confirm)this._controls.confirm.checked=false;
    if(this._controls.replace_learned_rate)this._controls.replace_learned_rate.checked=false;
    if(this._deleteWarning)this._deleteWarning.textContent=`Permanently delete “${record.name}” (${record.id.slice(-8)})${this._action==='delete_bottle'?' and all its fill and weighing history':''}. This cannot be undone.`;
    if(this._tareInfo)this._tareInfo.textContent=record.empty_g==null?'Empty bottle weight is unknown. Use Edit bottle to enter it before recording a measured gross weight.':'';
    if(this._rateInfo)this._rateInfo.textContent=record.rate_g_per_second==null?'No consumption rate yet.':`Current default: ${Number(record.rate_g_per_second).toPrecision(5)} g per active second · ${record.rate_source || 'unknown source'}.`;
    if(this._replaceRate)this._replaceRate.hidden=record.rate_source!=='learned';
  }
  _fields() { return Object.fromEntries(Object.entries(this._controls).map(([key,input])=>[key,input.type==='checkbox'?input.checked:input.value])); }
  async _service(service,data) { return this._hass.callWS({type:'call_service',domain:DOMAIN,service,service_data:{config_entry_id:this.config.entry_id,...data}}); }
  async _submit() {
    if(this._busy||!this._data||this._data.storage_error)return;
    this._error.textContent='';this._success.textContent='';
    let payload;try{payload=buildPayload(this._action,this._fields(),this._data.inventory,value(this._hass,this.config.entities,'power'));}catch(e){this._error.textContent=e.message;return;}
    this._setBusy(true);
    try{
      await this._service('manage_inventory',payload);
      this._dirty=false;
      this._success.textContent=`${ACTIONS[payload.action]} saved.${['install_bottle','store_bottle'].includes(payload.action)?' Scenting remains off; resume when ready.':''}`;
      try{await this._read();this._renderManager();}catch(e){this._data=null;this._error.textContent=`Saved, but refresh failed: ${e.message}. Refresh before another change.`;}
    }catch(e){this._error.textContent=`Not saved: ${e.message||String(e)}. Your entries are retained; refresh if another user changed this record.`;}
    finally{this._setBusy(false);}
  }
  async _acknowledge() {
    if(this._busy)return;this._setBusy(true);this._error.textContent='';
    try{await this._service('acknowledge_refill',{});this._success.textContent='Refill alert acknowledged.';}catch(e){this._error.textContent=e.message||String(e);}finally{this._setBusy(false);}
  }
  async _power(on) {
    if(this._busy)return;
    const entity=this.config.entities.power;if(!entity){this._error.textContent='No scenting switch configured.';return;}
    this._setBusy(true);this._error.textContent='';
    try{await this._hass.callWS({type:'call_service',domain:'switch',service:on?'turn_on':'turn_off',service_data:{entity_id:entity}});this._success.textContent=on?'Scenting resume requested.':'Scenting stop requested. Wait for Off before changing the bottle.';}
    catch(e){this._error.textContent=e.message||String(e);}finally{this._setBusy(false);}
  }
}
if(globalThis.customElements&&!customElements.get('aromatech-inventory-card'))customElements.define('aromatech-inventory-card',AromaTechInventoryCard);
if(globalThis.window){window.customCards=window.customCards||[];window.customCards.push({type:'aromatech-inventory-card',name:'AromaTech inventory',description:'Scent and bottle management using the AromaTech integration.'});}
