const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const enc = encodeURIComponent;
const statuses = {pending:'等待',running:'执行中',confirmed:'成绩已确认',skipped:'达标跳过',review:'待复核',unsupported:'未支持',error:'失败',paused:'已暂停'};
const stages = {ready:'待启动',discovering:'查询目录',running:'执行中',review:'待处理',paused:'已暂停',needs_login:'需要登录',error:'执行中断',complete:'已完成'};
const state = {unlocked:false,accounts:[],accountId:'',nodes:new Map(),tree:[],selected:null,view:'node',queue:null,queueId:null,queues:[],editing:null,busy:false,environmentBusy:false,revision:0};
let toastTimer, previewUrl, pollPending = false;

async function api(path, options = {}) {
  const headers = {'X-FIF-Client':'1', ...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api' + path, {...options,headers,credentials:'same-origin'});
  if (!response.ok) {
    let error = {}; try {error = await response.json();} catch {}
    if (response.status === 401) showUnlock();
    throw new Error(typeof error.detail === 'string' ? error.detail : `请求失败（${response.status}）`);
  }
  return response;
}
const json = async (path, options) => (await api(path, options)).json();
const post = (path, body = {}) => json(path, {method:'POST',body:JSON.stringify(body)});
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden=true;},5000); }
async function safely(work, errorId, button) {
  if (button) button.disabled = true;
  if (errorId) $(errorId).textContent = '';
  try { return await work(); } catch (error) { if (errorId) $(errorId).textContent = error.message; else toast(error.message); }
  finally { if (button) button.disabled = false; }
}
const current = () => state.accounts.find(a => a.id === state.accountId);
const key = n => JSON.stringify([n.kind,n.task_id,n.unit_id,n.id]);
function register(nodes) { for (const node of nodes) {node.key = key(node);state.nodes.set(node.key,node);} return nodes; }
function badge(value, label) { return `<span class="badge ${escape(value)}">${escape(label ?? statuses[value] ?? stages[value] ?? value)}</span>`; }
function openDialog(id) { $(id).showModal(); }
function closeDialog(dialog) { dialog.close(); if (dialog.id === 'login-dialog') { $('login-password').value='';$('login-token').value='';$('captcha-answer').value=''; } }
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click',() => closeDialog(button.closest('dialog'))));
$('login-dialog').addEventListener('close',() => {$('login-password').value='';$('login-token').value='';$('captcha-answer').value='';});
$('account-dialog').addEventListener('close',() => { $('edit-password').value='';$('voice-player').pause(); if (previewUrl) URL.revokeObjectURL(previewUrl);previewUrl=null; });

function showUnlock(exists = true) {
  state.unlocked=false;state.accounts=[];state.tree=[];state.nodes.clear();state.queue=null;state.queueId=null;state.busy=false;
  $('app-shell').hidden=true;$('unlock-shell').hidden=false;$('content').replaceChildren();
  $('confirmation-label').hidden=exists;$('master-confirmation').required=!exists;
  $('unlock-title').textContent=exists?'解锁账号库':'创建本地账号库';$('unlock-button').textContent=exists?'解锁':'创建账号库';
  document.querySelectorAll('dialog[open]').forEach(closeDialog);
}
async function bootstrap() {
  const data = await json('/bootstrap');
  if (!data.unlocked) {showUnlock(data.exists);return;}
  state.unlocked=true;$('unlock-shell').hidden=true;$('app-shell').hidden=false;
  state.environmentBusy=Boolean(data.environment_busy);
  await refreshAccounts();
  if (data.active_queue) {state.queueId=data.active_queue;state.queue=await json('/queues/'+data.active_queue);state.busy=state.queue.busy;state.view='queue';renderQueue();}
  else await loadTasks();
  updateHeader();
}
$('unlock-form').addEventListener('submit',event => {event.preventDefault();safely(async () => {
  await post('/unlock',{password:$('master-password').value,confirmation:$('master-confirmation').value});
  $('master-password').value='';$('master-confirmation').value='';await bootstrap();
},'unlock-error',$('unlock-button'));});
$('lock-button').addEventListener('click',() => safely(async () => {await post('/lock');showUnlock();}));

async function refreshAccounts() {
  const data=await json('/accounts');state.accounts=data.accounts;
  if (!current()) state.accountId=data.active || state.accounts[0]?.id || '';
  $('account-select').innerHTML=state.accounts.map(a=>`<option value="${escape(a.id)}">${escape(a.name)}</option>`).join('') || '<option>请添加账号</option>';
  $('account-select').value=state.accountId;updateHeader();
}
function updateHeader() {
  const account=current();$('account-state').textContent=account?.connected?`已登录 · ${account.display_name || account.username}`:'未登录';
  $('login-button').textContent=account?.connected?'重新登录':'登录';
  const busy=state.busy || state.environmentBusy;
  $('login-button').disabled=!account || busy;$('refresh-button').disabled=!account?.connected;
  $('lock-button').disabled=busy;$('settings-button').disabled=busy;$('accounts-button').disabled=busy;
  $('environment-button').disabled=state.busy;
  $('running-indicator').textContent=state.busy?'● 队列执行中':state.environmentBusy?'● 环境准备中':'';
}
$('account-select').addEventListener('change',() => safely(async () => {
  state.accountId=$('account-select').value;state.selected=null;state.tree=[];state.nodes.clear();state.revision++;
  await post(`/accounts/${enc(state.accountId)}/select`);updateHeader();await loadTasks();
}));
$('task-status').addEventListener('change',() => safely(loadTasks));
$('refresh-button').addEventListener('click',() => safely(loadTasks));
$('tree-search').addEventListener('input',renderTree);
function empty(title, message, action='') { $('content').innerHTML=`<div class="empty"><div class="empty-mark">◇</div><h1>${escape(title)}</h1><p class="muted">${escape(message)}</p>${action}</div>`; }
async function loadTasks() {
  state.view='node';state.selected=null;const account=current();const revision=++state.revision;
  if (!account) {state.tree=[];renderTree();empty('开始使用 FIF 口语','添加账号并导入参考音色，随后即可查询任务。','<button class="primary" id="empty-add">添加账号</button>');$('empty-add').onclick=showAccounts;return;}
  if (!account.connected) {state.tree=[];renderTree();empty('登录后查看任务','使用当前账号登录 FIF 口语，客户端会查询任务与课程目录。','<button class="primary" id="empty-login">登录 FIF 口语</button>');$('empty-login').onclick=showLogin;return;}
  empty('正在读取任务','请稍候…');
  try {
    const nodes=await json(`/catalog/${enc(account.id)}/tasks?status=${$('task-status').value}`);
    if (revision!==state.revision) return;
    state.nodes.clear();state.tree=register(nodes);renderTree();
    empty(nodes.length?'选择一个任务':'暂无任务',nodes.length?'从左侧查看课程或关卡。右键任一级目录，均可启动全自动完成。':'当前分类下没有任务。');
  } catch(error) {if(revision===state.revision){state.tree=[];renderTree();empty('暂时无法读取任务',error.message,'<button id="retry-tasks">重试</button>');$('retry-tasks').onclick=()=>safely(loadTasks);}}
}
function renderTree() {
  const search=$('tree-search').value.trim().toLowerCase();
  const matches=n=>n.title.toLowerCase().includes(search) || n.children?.some(matches);
  const row=node=>{
    if(search && !matches(node)) return '';
    return `<div class="tree-row ${node.kind} ${state.selected?.key===node.key?'selected':''}" data-node="${escape(node.key)}">
      <button class="expand" data-expand aria-label="${node.open?'收起':'展开'}${escape(node.title)}" ${node.kind==='level'?'disabled':''}>${node.open?'▾':'▸'}</button>
      <button class="tree-label" data-select>${escape(node.title)}</button></div>
      ${node.open || search?`<div class="tree-children">${(node.children || []).map(row).join('')}${node.error?`<p class="tree-error">${escape(node.error)}</p>`:''}</div>`:''}`;
  };
  $('task-tree').innerHTML=state.tree.map(row).join('') || '<p class="footnote">目录为空</p>';
}
async function loadChildren(node) {
  if(node.children || node.kind==='level') return;
  const accountId=state.accountId;let path=`/catalog/${enc(accountId)}/tasks/${enc(node.task_id)}`;
  if(node.kind==='unit') path+=`/units/${enc(node.unit_id)}`;
  try {const data=await json(path);if(accountId===state.accountId) {node.children=register(data);node.error='';}}
  catch(error) {node.error=error.message;throw error;}
}
async function selectNode(node) {
  state.selected=node;state.view='node';renderTree();renderNode(true);
  try {
    if(node.kind==='level' && !node.detail) node.detail=await json(`/catalog/${enc(state.accountId)}/levels/${enc(node.id)}`);
    else if(node.kind!=='level') {await loadChildren(node);node.open=true;}
  } finally {if(state.selected===node && state.view==='node'){renderTree();renderNode();}}
}
$('task-tree').addEventListener('click',event=>{
  const row=event.target.closest('[data-node]');if(!row)return;const node=state.nodes.get(row.dataset.node);if(!node)return;
  safely(async()=>{if(event.target.closest('[data-expand]')){node.open=!node.open;if(node.open)await loadChildren(node);renderTree();}else await selectNode(node);});
});
let contextNode;
$('task-tree').addEventListener('contextmenu',event=>{
  const row=event.target.closest('[data-node]');if(!row)return;event.preventDefault();contextNode=state.nodes.get(row.dataset.node);
  const menu=$('context-menu');menu.hidden=false;menu.style.left=Math.min(event.clientX,window.innerWidth-165)+'px';menu.style.top=Math.min(event.clientY,window.innerHeight-58)+'px';
});
document.addEventListener('click',()=>{$('context-menu').hidden=true;});
document.addEventListener('keydown',event=>{if(event.key==='Escape')$('context-menu').hidden=true;});
$('context-auto').addEventListener('click',()=>contextNode && safely(()=>startNode(contextNode)));

function renderNode(loading=false) {
  const node=state.selected;if(!node)return;
  const account=current();let metrics='';
  if(node.score!==null && node.score!==undefined && node.score!=='')metrics+=`<div class="metric"><div class="metric-caption">平台成绩</div><div class="metric-value">${escape(node.score)} / 100</div></div>`;
  if(node.complete!==null && node.complete!==undefined && node.complete!=='')metrics+=`<div class="metric"><div class="metric-caption">任务进度</div><div class="metric-value small">${escape(String(node.complete).replace(/%$/,''))}%</div></div>`;
  const parentNames=[...state.nodes.values()].filter(n=>n.key!==node.key && n.task_id===node.task_id && (n.kind==='task' || (node.kind==='level' && n.kind==='unit' && n.unit_id===node.unit_id))).map(n=>n.title);
  let body='';
  if(loading)body='<p class="muted">正在查询完整内容…</p>';
  else if(node.kind==='level'){
    body=node.detail?.reason?`<p class="footnote">${escape(node.detail.reason)}</p>`:'';
    for(const group of node.detail?.groups || [])body+=`<section class="prompt-list"><h3>${escape(group.name)} · ${escape(group.kind)}</h3>${group.prompts.map((p,i)=>`<div class="item-card"><span class="ordinal">${String(i+1).padStart(2,'0')}</span><div class="prompt-copy">${escape(p.text)}${p.translation?`<div class="translation">${escape(p.translation)}</div>`:''}</div>${p.audio?`<audio class="prompt-audio" controls preload="none" src="${escape(p.audio)}"></audio>`:''}</div>`).join('')}</section>`;
    if(!node.detail?.groups?.length)body+='<p class="muted">暂无题目内容。</p>';
  } else {
    body=`<p class="muted small">${(node.children||[]).length} 个${node.kind==='task'?'课程':'关卡'}</p>`;
    body+=(node.children||[]).map((n,i)=>`<div class="item-card"><span class="ordinal">${String(i+1).padStart(2,'0')}</span><span class="title">${escape(n.title)}</span>${n.score!==null&&n.score!==undefined?`<span class="score-inline">${escape(n.score)} 分</span>`:''}<button class="quiet" data-open="${escape(n.key)}">查看</button></div>`).join('');
    if(node.error)body+=`<p class="error">${escape(node.error)}</p>`;
  }
  $('content').innerHTML=`${parentNames.length?`<div class="breadcrumbs">${escape(parentNames.join(' / '))}</div>`:''}
    <div class="eyebrow">${{task:'口语任务',unit:'课程',level:'关卡'}[node.kind]}</div>
    <div class="heading-row"><h1>${escape(node.title)}</h1><button id="node-auto" class="primary" ${state.busy?'disabled':''}>全自动完成</button></div>
    ${metrics?`<div class="metrics">${metrics}</div>`:''}${node.end_time?`<p class="footnote">截止时间 ${escape(node.end_time)}</p>`:''}
    <div class="auto-options"><select id="auto-voice" aria-label="本次参考音色"><option value="">无需参考音色（Edge / 系统语音）</option>${(account?.voices||[]).map(v=>`<option value="${escape(v.id)}" ${v.id===account.default_voice?'selected':''}>${escape(v.name)}（仅本地模型使用）</option>`).join('')}</select><label class="check"><input id="skip-qualified" type="checkbox" checked>跳过平台已达目标的关卡</label></div>${body}`;
  $('node-auto').onclick=()=>safely(()=>startNode(node),null,$('node-auto'));
  $('content').querySelectorAll('[data-open]').forEach(button=>button.onclick=()=>safely(()=>selectNode(state.nodes.get(button.dataset.open))));
}
async function startNode(node) {
  const voiceId=$('auto-voice')?.value ?? current()?.default_voice ?? '';
  const skip=$('skip-qualified')?.checked ?? true;
  const scope={kind:node.kind,task_id:node.task_id,unit_id:node.unit_id || '',level_id:node.kind==='level'?node.id:'',title:node.title};
  state.queue=await post('/queues',{account_id:state.accountId,scope,voice_id:voiceId || null,skip_qualified:skip});
  state.queueId=state.queue.id;state.busy=state.queue.busy;state.view='queue';await refreshQueues();renderQueue();updateHeader();
}
async function refreshQueues() {if(state.accountId)state.queues=await json('/queues?account_id='+enc(state.accountId));}
$('queue-view').addEventListener('click',()=>safely(async()=>{state.view='queue';await refreshQueues();if(!state.queueId && state.queues.length){state.queueId=state.queues[0].id;state.queue=await json('/queues/'+state.queueId);}renderQueue();}));
function renderQueue() {
  const view=state.queue;
  if(!view){empty('执行队列','在任务、课程或关卡上选择「全自动完成」，即可开始。');return;}
  const data=view.data;const counts={};for(const j of data.jobs)counts[j.state]=(counts[j.state]||0)+1;
  const processed=data.jobs.filter(j=>!['pending','running','paused'].includes(j.state)).length;
  const account=state.accounts.find(a=>a.id===data.context.account_id);
  const oldLog=$('queue-log');const scroll=oldLog?.scrollTop || 0;const atBottom=!oldLog || oldLog.scrollHeight-oldLog.clientHeight-scroll<32;
  const options=state.queues.map(q=>q.id===view.id?{...q,stage:data.stage}:q);if(!options.some(q=>q.id===view.id))options.unshift({id:view.id,title:data.title,stage:data.stage});
  $('content').innerHTML=`<div class="eyebrow">全自动完成</div><div class="heading-row"><h1>${escape(data.title)}</h1>${badge(data.stage,stages[data.stage])}</div>
    <p class="muted small">${escape(account?.name || '绑定账号')} · 逐关执行 · 进度自动保存</p>
    <div class="queue-controls"><select id="queue-picker" aria-label="历史队列" ${state.busy?'disabled':''}>${options.map(q=>`<option value="${escape(q.id)}" ${q.id===view.id?'selected':''}>${escape(q.title)} · ${escape(stages[q.stage]||q.stage)}</option>`).join('')}</select>
    <button id="queue-pause" ${view.busy?'':'disabled'}>暂停</button><button id="queue-resume" class="primary" ${state.busy?'disabled':''}>继续 / 重试未完成关卡</button></div>
    <progress class="queue-progress" value="${processed}" max="${Math.max(data.jobs.length,1)}" aria-label="关卡处理进度"></progress>
    <p class="muted small">已处理 ${processed}/${data.jobs.length} · 确认 ${counts.confirmed||0} · 达标跳过 ${counts.skipped||0} · 待复核 ${counts.review||0} · 未支持 ${counts.unsupported||0} · 失败 ${counts.error||0}</p>
    <p class="footnote">当前优化目标 ${escape(data.settings?.target_score)} 分。关闭网页后后台继续运行；重新打开可查看进度。</p>
    ${view.error?`<p class="error">${escape(view.error)}</p>`:''}
    ${data.discovery_issues?.length?`<div class="discovery-issues">${data.discovery_issues.map(i=>`${escape(i.name)}：${escape(i.reason)}`).join('<br>')}</div>`:''}
    <div class="table-wrap"><table><thead><tr><th>课程</th><th>关卡</th><th>状态</th><th>分数</th><th>说明</th></tr></thead><tbody>${data.jobs.map(j=>`<tr><td>${escape(j.unit_name || '当前课程')}</td><td>${escape(j.name)}</td><td>${badge(j.state)}</td><td>${escape(j.score ?? '—')}</td><td>${escape(j.message)}${j.report_id?`<br>报告 ${escape(j.report_id)}`:''}</td></tr>`).join('') || '<tr><td colspan="5">正在读取目录…</td></tr>'}</tbody></table></div>
    <div class="section-heading"><h3>运行日志</h3><span class="muted small">${view.busy?'自动更新':'本轮记录'}</span></div><pre id="queue-log" class="log-box">${escape(view.logs.join('\n') || '重启服务后可继续读取已保存的关卡进度。')}</pre>`;
  $('queue-log').scrollTop=atBottom?$('queue-log').scrollHeight:scroll;
  $('queue-pause').onclick=()=>safely(async()=>{await post(`/queues/${state.queueId}/pause`);toast('正在暂停并保存进度…');},null,$('queue-pause'));
  $('queue-resume').onclick=()=>safely(async()=>{state.queue=await post(`/queues/${state.queueId}/resume`);state.busy=state.queue.busy;renderQueue();updateHeader();},null,$('queue-resume'));
  $('queue-picker').onchange=()=>safely(async()=>{state.queueId=$('queue-picker').value;state.queue=await json('/queues/'+state.queueId);renderQueue();});
}
async function pollQueue() {
  if(!state.unlocked || !state.queueId || !state.busy || pollPending)return;pollPending=true;
  try {
    state.queue=await json('/queues/'+state.queueId);state.busy=state.queue.busy;
    if(state.view==='queue')renderQueue();updateHeader();
    if(!state.busy){await refreshQueues();if(state.view==='node')renderNode();$('footer-state').textContent=`${state.queue.data.title} · ${stages[state.queue.data.stage]||'已结束'}`;}
  } catch(error) {toast(error.message);}finally{pollPending=false;}
}
setInterval(pollQueue,1000);

function showLogin() {
  const account=current();if(!account){showAccounts();return;}
  $('login-account').textContent=`${account.name} · ${account.username}`;$('login-password').value='';$('login-password').placeholder=account.has_password?'留空使用已保存密码':'';
  $('login-token').value='';$('login-error').textContent='';$('captcha-image').removeAttribute('src');$('captcha-area').hidden=true;$('captcha-answer').value='';$('login-mode').value='password';switchLogin();openDialog('login-dialog');
}
function switchLogin(){const token=$('login-mode').value==='token';$('login-token-label').hidden=!token;$('login-password-label').hidden=token;$('captcha-area').hidden=token || !$('captcha-image').getAttribute('src');}
$('login-button').onclick=showLogin;$('login-mode').onchange=switchLogin;
$('login-form').addEventListener('submit',event=>{event.preventDefault();safely(async()=>{
  const accountId=state.accountId;const token=$('login-mode').value==='token';
  const result=await post(`/accounts/${enc(accountId)}/login`,{password:token?'':$('login-password').value,answer:token?'':$('captcha-answer').value,token:token?$('login-token').value:''});
  if(!result.connected){$('captcha-image').src=result.captcha;$('captcha-area').hidden=false;$('login-error').textContent=result.message;$('captcha-answer').value='';$('captcha-answer').focus();return;}
  closeDialog($('login-dialog'));await refreshAccounts();toast('登录成功');await loadTasks();
},'login-error',$('login-submit'));});

function showAccounts(){state.editing=state.accountId || null;renderAccountEditor();openDialog('account-dialog');}
$('accounts-button').onclick=showAccounts;
function renderAccountEditor(){
  const account=state.accounts.find(a=>a.id===state.editing);
  $('account-list').innerHTML=state.accounts.map(a=>`<button data-edit-account="${escape(a.id)}" class="${a.id===state.editing?'selected':''}">${escape(a.name)}</button>`).join('');
  $('account-list').querySelectorAll('button').forEach(b=>b.onclick=()=>{state.editing=b.dataset.editAccount;renderAccountEditor();});
  $('edit-name').value=account?.name || '';$('edit-username').value=account?.username || '';$('edit-password').value='';$('edit-password').placeholder=account?.has_password?'已保存，留空保持':'';$('clear-password').checked=false;
  $('delete-account').disabled=!account;$('voice-file').disabled=!account;$('voice-form').querySelector('button').disabled=!account;$('account-error').textContent='';
  $('voice-list').innerHTML=(account?.voices||[]).map(v=>`<div class="voice-item"><span>${escape(v.name)}</span>${v.id===account.default_voice?'<span class="badge confirmed">默认</span>':''}<button data-voice-action="play" data-voice="${escape(v.id)}">试听</button><button data-voice-action="default" data-voice="${escape(v.id)}">设为默认</button><button data-voice-action="delete" data-voice="${escape(v.id)}" class="danger">移除</button></div>`).join('');
}
$('new-account').onclick=()=>{state.editing=null;renderAccountEditor();$('edit-name').focus();};
$('account-form').addEventListener('submit',event=>{event.preventDefault();safely(async()=>{
  const identity=state.editing;const body={name:$('edit-name').value,username:$('edit-username').value,password:$('clear-password').checked?'':$('edit-password').value || null};
  const result=await json(identity?`/accounts/${enc(identity)}`:'/accounts',{method:identity?'PUT':'POST',body:JSON.stringify(body)});
  state.editing=result.id;await refreshAccounts();renderAccountEditor();await loadTasks();toast('账号已保存');
},'account-error',event.submitter);});
$('delete-account').onclick=()=>safely(async()=>{
  if(!state.editing || !confirm('移除这个账号？历史挑战进度仍保留在本地。'))return;
  await json(`/accounts/${enc(state.editing)}`,{method:'DELETE'});await refreshAccounts();state.editing=state.accountId;renderAccountEditor();await loadTasks();
},'account-error');
$('voice-form').addEventListener('submit',event=>{event.preventDefault();safely(async()=>{
  if(!$('voice-file').files[0])return;const body=new FormData();body.append('file',$('voice-file').files[0]);
  await json(`/accounts/${enc(state.editing)}/voices`,{method:'POST',body});$('voice-file').value='';await refreshAccounts();renderAccountEditor();if(state.selected)renderNode();toast('音色已导入');
},'account-error',event.submitter);});
$('voice-list').addEventListener('click',event=>{const button=event.target.closest('[data-voice-action]');if(!button)return;safely(async()=>{
  const base=`/accounts/${enc(state.editing)}/voices/${enc(button.dataset.voice)}`;
  if(button.dataset.voiceAction==='play'){
    const blob=await (await api(base+'/audio')).blob();if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(blob);$('voice-player').src=previewUrl;$('voice-player').hidden=false;await $('voice-player').play();return;
  }
  if(button.dataset.voiceAction==='delete'){if(!confirm('移除这个参考音色文件？'))return;await json(base,{method:'DELETE'});}else await post(base+'/default');
  await refreshAccounts();renderAccountEditor();if(state.selected)renderNode();
},'account-error',button);});

$('settings-button').onclick=()=>safely(async()=>{
  const config=await json('/settings');const form=$('settings-form');for(const [key,value] of Object.entries(config.settings)){const input=form.elements.namedItem(key);if(input.type==='checkbox')input.checked=value==='on';else input.value=value;}
  form.elements.namedItem('ffmpeg').value=config.ffmpeg;$('settings-error').textContent='';openDialog('settings-dialog');
});
$('settings-form').addEventListener('submit',event=>{event.preventDefault();safely(async()=>{
  const settings={};const form=event.target;for(const input of form.elements){if(!input.name || input.name==='ffmpeg')continue;settings[input.name]=input.type==='checkbox'?(input.checked?'on':'off'):input.value;}
  await json('/settings',{method:'PUT',body:JSON.stringify({settings,ffmpeg:form.elements.namedItem('ffmpeg').value})});closeDialog($('settings-dialog'));toast('模型设置已保存，继续队列时应用于未完成题目。');
},'settings-error',event.submitter);});

safely(bootstrap,'unlock-error');

const acceptedModelLicenses=new Set();
let environmentRequest=false;
async function refreshEnvironment(){
  const data=await json('/environment');state.environmentBusy=data.task.busy;updateHeader();
  const task=data.task;
  $('environment-resources').innerHTML=data.resources.map(item=>{
    const verifiedRuntime=item.id==='runtime' && task.component==='check' && task.result?.runtime;
    const found=verifiedRuntime || item.found;
    return `<div class="item-card"><div class="title"><h3>${escape(item.name)}</h3><p class="footnote">${found?'已找到文件 / 检测记录':'待准备'} · ${escape(item.description)}</p>${item.path?`<p class="footnote">${escape(item.path)}</p>`:''}
      ${item.license?`<p class="footnote">约 ${(item.size/1024**2).toFixed(0)} MB · <a href="${escape(item.license_url)}" target="_blank" rel="noreferrer">${escape(item.license)}</a></p><label class="check"><input type="checkbox" data-license="${escape(item.id)}" ${acceptedModelLicenses.has(item.id)?'checked':''} ${task.busy?'disabled':''}>已阅读并同意此模型许可</label>`:''}</div><button data-install="${escape(item.id)}" ${task.busy?'disabled':''}>准备 / 修复</button></div>`;
  }).join('');
  const bar=$('environment-progress');
  if(task.total){bar.value=Math.min(100,task.received*100/task.total);}else if(task.busy){bar.removeAttribute('value');}else bar.value=task.stage==='completed'?100:0;
  const bytes=task.total?` · ${(task.received/1024**2).toFixed(1)} / ${(task.total/1024**2).toFixed(1)} MB`:'';
  $('environment-message').textContent=task.message+bytes;
  if(task.component==='check' && task.result?.runtime_error)$('environment-message').textContent='TTS 环境尚不可用，可点击对应的准备按钮。\n'+task.result.runtime_error;
  $('environment-logs').textContent=task.logs.join('\n');$('environment-logs').scrollTop=$('environment-logs').scrollHeight;
  $('environment-check').disabled=task.busy;$('environment-cancel').disabled=!task.busy;
  $('environment-edge-test').disabled=task.busy;
}
async function installEnvironment(component){
  await post('/environment',{component,accept_license:acceptedModelLicenses.has(component)});await refreshEnvironment();
}
$('environment-button').onclick=()=>safely(async()=>{await refreshEnvironment();openDialog('environment-dialog');});
$('environment-resources').addEventListener('change',event=>{const id=event.target.dataset.license;if(id){if(event.target.checked)acceptedModelLicenses.add(id);else acceptedModelLicenses.delete(id);}});
$('environment-resources').addEventListener('click',event=>{const button=event.target.closest('[data-install]');if(button)safely(()=>installEnvironment(button.dataset.install),null,button);});
$('environment-check').onclick=()=>safely(()=>installEnvironment('check'));
$('environment-edge-test').onclick=()=>safely(()=>installEnvironment('edge_test'));
$('refresh-edge-voices').onclick=()=>safely(async()=>{
  const voices=await json('/edge/voices');$('edge-voice-options').innerHTML=voices.map(v=>`<option value="${escape(v.name)}">${escape(v.locale)} · ${escape(v.gender)}</option>`).join('');toast(`已加载 ${voices.length} 个英文音色。`);
},'settings-error',$('refresh-edge-voices'));
$('environment-cancel').onclick=()=>safely(async()=>{await post('/environment/cancel');toast('正在取消安装并保留下载进度…');});
setInterval(async()=>{
  if(!state.unlocked || !state.environmentBusy || environmentRequest)return;
  environmentRequest=true;
  try{await refreshEnvironment();}catch(error){toast(error.message);}finally{environmentRequest=false;}
},1000);
