/* MyLife 原型 · 语音朗读（服务器上的腾讯云 TTS，退回系统自带朗读）和声音设置。由 mylife-prototype.html 在
   “工具”之后、“屏幕栈”之前引入：用到前面的 $、store、S，后面的代码用这里的 speak、TTS 等。 */
'use strict';
/* ================= 语音朗读 =================
   部署在服务器上时：服务器调用腾讯云语音合成（TTS 服务后台固定，用户只选音色和语速，密钥只在服务器上）；
   直接打开本地文件或服务器不可用时：退回手机自带的语音合成。 */
var TTS={ok:'speechSynthesis' in window&&'SpeechSynthesisUtterance' in window,voices:[],cur:null,unlocked:false,BASE:4.0,
  cloud:null,byId:{},audio:null,url:null,seq:0};
var FEMALE=/(female|女|xiaoxiao|huihui|tingting|yaoyao|xiaoyi|meijia|sinji|lili|xiaobei|xiaoni|xiaochen|xiaohan|xiaomo|xiaorui|xiaoshuang|xiaoxuan|xiaoyan|xiaoyou|xiaozhen|hanhan|yating|shanshan|linlin)/i;
var MALE=/(\bmale\b|男|yunxi|yunyang|yunjian|yunhao|yunye|yunze|yunfeng|kangkang|zhiwei|liang|danny)/i;
function loadVoices(){if(!TTS.ok)return;TTS.voices=speechSynthesis.getVoices().filter(function(v){return /^(zh|cmn)/i.test(v.lang)})}
if(TTS.ok){loadVoices();try{speechSynthesis.addEventListener('voiceschanged',loadVoices)}catch(e){speechSynthesis.onvoiceschanged=loadVoices}}
function guessGender(v){return FEMALE.test(v.name)?'female':MALE.test(v.name)?'male':'unknown'}
function pickVoice(g){
  var vs=TTS.voices.slice().sort(function(a,b){return (/zh-CN|cmn/i.test(b.lang)?1:0)-(/zh-CN|cmn/i.test(a.lang)?1:0)});
  var hit=vs.filter(function(v){return guessGender(v)===g})[0];
  return {voice:hit||vs[0]||null,fallback:!hit};
}
function countChars(t){var m=String(t).match(/[一-龥A-Za-z0-9]/g);return m?m.length:0}
function rateFor(vname,cps){var base=S.calib[vname||'_']||TTS.BASE;return Math.max(0.5,Math.min(2,cps/base))}
// 朗读前去掉表情符号（气泡里照常显示）
var EMOJI=/[\uD83C-\uDBFF][\uDC00-\uDFFF]|[☀-➿]|[️‍]/g;
function cleanText(t){return String(t).replace(EMOJI,'').trim()}

/* 云端音色（腾讯云） */
function cloudVoice(pid){if(!TTS.cloud)return null;var c=S.voice[pid];return TTS.byId[(c.vt||{})[c.gender]]||null}
function voiceLabel(pid){var c=S.voice[pid],v=cloudVoice(pid);return (v?v.name+' · ':'')+(c.gender==='male'?'男声':'女声')}
function fixVoicePrefs(){
  var d=TTS.cloud.defaults||{};
  ['bange','pingzuo'].forEach(function(id){var c=S.voice[id];c.vt=c.vt||{};
    ['male','female'].forEach(function(g){if(TTS.byId[c.vt[g]])return;var def=(d[id]||{})[g];
      c.vt[g]=TTS.byId[def]?def:(TTS.cloud.voices.filter(function(v){return v.gender===g})[0]||{}).id})});
  store.set('voice',S.voice);
}
function loadCloud(){
  if(!/^https?:$/.test(location.protocol)||!window.fetch)return;
  fetch('/api/voices',{cache:'no-store'}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(d){
    if(!d||!d.voices||!d.voices.length)return;
    TTS.cloud=d;TTS.byId={};d.voices.forEach(function(v){TTS.byId[v.id]=v});fixVoicePrefs();
    renderSetVoice();if($('#sh-voice').classList.contains('on'))openVoiceSheet(vSheetPid);
  }).catch(function(){});
}
function silentUrl(){return URL.createObjectURL(encodeWav(new Float32Array(800),8000))}
try{TTS.audio=new Audio();TTS.audio.preload='auto'}catch(e){}

function markRow(row,on){if(!row)return;row.classList.toggle('speaking',on);var b=row.querySelector('.spk');if(b)b.classList.toggle('on',on)}
function stopSpeak(){
  TTS.seq++;
  if(TTS.audio){try{TTS.audio.pause()}catch(e){}}
  if(TTS.ok){try{speechSynthesis.cancel()}catch(e){}}
  $$('.row.speaking').forEach(function(r){r.classList.remove('speaking')});$$('.spk.on').forEach(function(b){b.classList.remove('on')});
}
function noteMeasured(pid,obs){S.measured[pid]=obs;var mv=$('#v-measured');if(mv&&vSheetPid===pid)mv.textContent=measuredText(pid)}
// opts.cps 改语速（彩虹卡用稍慢一点）
function speak(text,pid,msgId,opts){
  stopSpeak();opts=opts||{};
  if(cloudVoice(pid)&&TTS.audio)speakCloud(text,pid,msgId,opts);else speakLocal(text,pid,msgId,opts);
}
function rowFor(id){return id?msgs.querySelector('[data-id="'+id+'"]'):null}
function speakCloud(text,pid,msgId,opts){
  var v=cloudVoice(pid),cfg=S.voice[pid],my=TTS.seq,t=cleanText(text),row=rowFor(msgId);markRow(row,true);
  function end(){if(my===TTS.seq)markRow(row,false)}
  fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({v:v.id,cps:opts.cps||cfg.cps,p:pid,t:t})})
    .then(function(r){if(r.ok)return r.blob();return r.json().catch(function(){return {}}).then(function(e){e.status=r.status;throw e})})
    .then(function(blob){
      if(my!==TTS.seq)return;
      var a=TTS.audio;if(TTS.url)URL.revokeObjectURL(TTS.url);TTS.url=URL.createObjectURL(blob);
      a.onended=function(){var d=a.duration,n=countChars(t);if(my===TTS.seq&&!opts.cps&&isFinite(d)&&d>0.8&&n>=6)noteMeasured(pid,n/d);end()};
      a.onerror=end;
      a.src=TTS.url;var p=a.play();
      if(p&&p.catch)p.catch(function(err){if(err&&err.name==='NotAllowedError'&&my===TTS.seq)toast('浏览器拦住了自动播放，点回复旁的喇叭就能听');end()});
    })
    .catch(function(e){
      if(my!==TTS.seq)return;
      if(e&&e.error==='DailyCap'){toast('今天的朗读额度用完了，明天再听');end();return}
      if(e&&e.status===429){toast('点得有点快，稍等几秒再听');end();return}
      markRow(row,false);toast('云端语音暂时连不上，先用手机自带的声音');speakLocal(text,pid,msgId,opts);
    });
}
function speakLocal(text,pid,msgId,opts){
  if(!TTS.ok){toast('这个浏览器不支持语音朗读');return}
  var cfg=S.voice[pid],pv=pickVoice(cfg.gender),u=new SpeechSynthesisUtterance(cleanText(text));
  var vname=pv.voice?pv.voice.name:'_';
  u.lang='zh-CN';if(pv.voice)u.voice=pv.voice;
  u.rate=rateFor(vname,opts.cps||cfg.cps);
  u.pitch=(pv.fallback&&cfg.gender==='male')?0.7:(pv.fallback&&cfg.gender==='female'?1.25:1);
  var t0=0,row=rowFor(msgId);
  u.onstart=function(){t0=performance.now();markRow(row,true)};
  u.onend=function(){
    markRow(row,false);
    var dur=(performance.now()-t0)/1000,n=countChars(u.text);
    if(t0&&!opts.cps&&dur>0.8&&n>=6){
      var obs=n/dur,base=obs/u.rate;
      S.calib[vname]=S.calib[vname]?(S.calib[vname]*0.5+base*0.5):base;store.set('calib',S.calib);
      noteMeasured(pid,obs);
    }
  };
  speechSynthesis.speak(u);
}
function measuredText(pid){var m=S.measured[pid];return m?'上一句实测：每秒 '+m.toFixed(1)+' 字。'+(TTS.cloud?'':'会自动校准，越说越接近目标。'):'说完一句后，这里会显示实测的语速。'}
// iOS / 微信需要在用户第一次点击时“解锁”声音，之后自动朗读才能出声
document.addEventListener('click',function(){
  if(TTS.unlocked)return;TTS.unlocked=true;
  if(TTS.audio){try{TTS.audio.src=silentUrl();var p=TTS.audio.play();if(p&&p.catch)p.catch(function(){})}catch(e){}}
  if(TTS.ok){try{var u=new SpeechSynthesisUtterance(' ');u.volume=0;speechSynthesis.speak(u)}catch(e){}}
},true);

var ICON_VOL='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/></svg>';
var ICON_MUTE='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M17 9l5 6M22 9l-5 6"/></svg>';
function paintVoiceBtn(){var vc=$('#msgs .vchoice');if(vc&&S.autoRead!==null)vc.remove();var b=$('#btn-voice');b.innerHTML=S.autoRead?ICON_VOL:ICON_MUTE;b.classList.toggle('off',!S.autoRead);b.setAttribute('aria-label',S.autoRead?'自动朗读：开':'自动朗读：关')}
function setAutoRead(v){S.autoRead=v;store.set('autoRead',v);paintVoiceBtn();renderSetVoice();if(!v)stopSpeak()}
$('#btn-voice').addEventListener('click',function(){setAutoRead(!S.autoRead);toast(S.autoRead?'回复会自动读出来':'已关闭自动朗读，可以点每条回复旁的喇叭单独听')});

/* 声音设置弹层 */
var vSheetPid='bange';
var SAMPLE={bange:'你说你不在乎。可真不在乎的人，不会把这件事讲三遍。',pingzuo:'不急。你慢慢说，想到哪儿说到哪儿。'};
var ICON_OK='<svg class="ok" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
function renderVoiceList(pid){
  var box=$('#v-list');if(!TTS.cloud){box.style.display='none';box.innerHTML='';return}
  var c=S.voice[pid];box.style.display='';
  box.innerHTML=TTS.cloud.voices.filter(function(v){return v.gender===c.gender}).map(function(v){
    return '<button class="item'+(c.vt[c.gender]===v.id?' sel':'')+'" data-vid="'+esc(v.id)+'"><div class="grow">'+esc(v.name)+'<small>'+esc(v.desc)+'</small></div>'+ICON_OK+'</button>'}).join('');
}
function openVoiceSheet(pid){
  vSheetPid=pid;var c=S.voice[pid];
  $('#v-title').textContent=P[pid].name+'（'+P[pid].kind+'）的声音';
  $('#v-desc').textContent=TTS.cloud?'每个人格可以单独选音色、调快慢。点一个音色就能试听。':'每个人格可以单独选男声或女声，调快慢。';
  $$('#v-gender button').forEach(function(b){b.classList.toggle('on',b.dataset.g===c.gender)});
  renderVoiceList(pid);
  $('#v-cps').value=c.cps;$('#v-cps-val').textContent=c.cps.toFixed(1)+' 字/秒';
  $('#v-auto').checked=S.autoRead;$('#v-measured').textContent=measuredText(pid);
  if(TTS.cloud)$('#v-note').textContent='声音由腾讯云语音合成生成。每个音色的语速都实测校准过，滑杆上的数字就是每秒实际读出的字数。';
  else{var pv=pickVoice(c.gender);
    $('#v-note').textContent=(!TTS.ok?'这个浏览器不支持语音朗读。':pv.fallback&&TTS.voices.length?'本机没有找到'+(c.gender==='male'?'男':'女')+'声中文音色，暂用变调代替。':'')+'现在用的是手机自带的语音合成（连不上服务器时的备用方案）。'}
  openSheet('#sh-voice');
}
$('#v-gender').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;S.voice[vSheetPid].gender=b.dataset.g;store.set('voice',S.voice);openVoiceSheet(vSheetPid);renderSetVoice()});
$('#v-list').addEventListener('click',function(e){var b=e.target.closest('[data-vid]');if(!b)return;var c=S.voice[vSheetPid];c.vt[c.gender]=b.dataset.vid;store.set('voice',S.voice);renderVoiceList(vSheetPid);renderSetVoice();speak(SAMPLE[vSheetPid],vSheetPid,null)});
$('#v-cps').addEventListener('input',function(){var v=+this.value;S.voice[vSheetPid].cps=v;store.set('voice',S.voice);$('#v-cps-val').textContent=v.toFixed(1)+' 字/秒';renderSetVoice()});
$('#v-auto').addEventListener('change',function(){setAutoRead(this.checked)});
$('#v-try').addEventListener('click',function(){speak(SAMPLE[vSheetPid],vSheetPid,null)});
function renderSetVoice(){
  var box=$('#set-voice');if(!box)return;box.innerHTML='';
  ['bange','pingzuo'].forEach(function(id){var c=S.voice[id],b=document.createElement('button');b.className='item';
    b.innerHTML='<div class="grow">'+P[id].name+'（'+P[id].kind+'）<small>'+esc(voiceLabel(id))+' · 每秒 '+c.cps.toFixed(1)+' 字</small></div><span class="tag">调整</span>';
    b.addEventListener('click',function(){openVoiceSheet(id)});box.appendChild(b)});
  var a=document.createElement('label');a.className='item';a.innerHTML='<div class="grow">回复自动朗读<small>关掉后可以点每条回复旁的喇叭单独听</small></div><input type="checkbox" class="check">';
  var cb=a.querySelector('input');cb.checked=S.autoRead;cb.addEventListener('change',function(){setAutoRead(cb.checked)});box.appendChild(a);
}
