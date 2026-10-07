const STATE_KEY = "toss_state";
const STATE_BAK_KEY = "toss_state_bak";
const CONFIG_KEY = "toss_config";
const RUNTIME_KEY = "toss_runtime";
const LINE_STATE_KEY = "toss_line_state";
const LINE_STATE_BAK_KEY = "toss_line_state_bak";
const TOSS_URL = "https://www.cm1.eprs.jp/TOSS/web/view/user/c005RsvEmptyState.html";
const DAY_MAX = 13;
const HOURS = [9,13,17];
const WD = ["月","火","水","木","金","土","日"];
const ERRORS = ["TOSS取得エラー","お知らせ取得エラー","TOSSページ構造エラー","状態ファイルエラー","LINE送信エラー"];
const MAX_BODY = 262144;
const STALE_MS = 600000;

function json(data,status){return new Response(JSON.stringify(data),{status:status||200,headers:{"content-type":"application/json; charset=utf-8","cache-control":"no-store"}});}
function nowIso(){return new Date().toISOString();}
function today(){
  const p=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Tokyo",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()),o={};
  for(const x of p)o[x.type]=x.value;
  return o.year+"-"+o.month+"-"+o.day;
}
function dateOk(v){if(typeof v!=="string"||!/^\d{4}-\d{2}-\d{2}$/.test(v))return false;const a=v.split("-").map(Number),d=new Date(Date.UTC(a[0],a[1]-1,a[2]));return d.getUTCFullYear()===a[0]&&d.getUTCMonth()===a[1]-1&&d.getUTCDate()===a[2];}
function addDay(v,n){const a=v.split("-").map(Number);return new Date(Date.UTC(a[0],a[1]-1,a[2])+n*86400000).toISOString().slice(0,10);}
function weekday(v){const a=v.split("-").map(Number);return (new Date(Date.UTC(a[0],a[1]-1,a[2])).getUTCDay()+6)%7;}
function label(v){const a=v.split("-");return "▦ "+a[0]+"/"+a[1]+"/"+a[2]+"（"+WD[weekday(v)]+"）";}
function fresh(v){if(!v)return"取得時刻不明";const d=new Date(v.indexOf("T")>=0?v:v.replace(" ","T")+"+09:00");if(Number.isNaN(d.getTime()))return"取得時刻不明";const m=Math.max(0,Math.floor((Date.now()-d.getTime())/60000));if(m<1)return"1分未満前";if(m<60)return m+"分前";const h=Math.floor(m/60),r=m%60;return h+"時間"+(r?r+"分":"")+"前";}
async function read(key,fallback){const v=await TOSS_KV.get(key);if(v===null)return fallback;return JSON.parse(v);}
async function tossState(){let s=null,b=false;try{s=await read(STATE_KEY,null)}catch{}if(!s?.statuses){try{s=await read(STATE_BAK_KEY,null);b=true}catch{}}if(!s?.statuses)throw Error("TOSS状態を読み取れません。");return{state:s,backup:b};}
function baseLine(){return{version:1,updated_at:nowIso(),monitoring:{enabled:false,start_date:null,end_date:null,enabled_at:null},operation:{feature:null,step:null,start_date:null,expires_at:null},facility_icons:{},processed_event_ids:[],last_line_error:null,last_line_send_at:null,last_seen_state_saved_at:null,last_toss_alert_signature:null,last_toss_runtime_signature:null,monitor_stale_notified:false};}
async function lineState(){let s=null;try{s=await read(LINE_STATE_KEY,null)}catch{}if(!s){try{s=await read(LINE_STATE_BAK_KEY,null)}catch{}}const b=baseLine();if(!s||typeof s!=="object")return b;return{...b,...s,monitoring:{...b.monitoring,...(s.monitoring||{})},operation:{...b.operation,...(s.operation||{})},facility_icons:s.facility_icons&&typeof s.facility_icons==="object"?s.facility_icons:{},processed_event_ids:Array.isArray(s.processed_event_ids)?s.processed_event_ids.slice(-200):[]};}
async function saveLine(s){const old=await TOSS_KV.get(LINE_STATE_KEY);if(old!==null)await TOSS_KV.put(LINE_STATE_BAK_KEY,old);const n={...s,updated_at:nowIso()};await TOSS_KV.put(LINE_STATE_KEY,JSON.stringify(n));return n;}
async function runtime(){try{const s=await read(RUNTIME_KEY,{});return s&&typeof s==="object"?s:{}}catch{return{}}}
async function config(){try{const s=await read(CONFIG_KEY,{monitor_enabled:false});return{monitor_enabled:s.monitor_enabled===true,updated_at:s.updated_at||null}}catch{return{monitor_enabled:false}}}
async function setConfig(on){await TOSS_KV.put(CONFIG_KEY,JSON.stringify({monitor_enabled:on===true,updated_at:nowIso()}));}
function hour(v){const m=String(v||"").match(/(?:^|\D)(\d{1,2})(?=\s*(?:時|:|：))/);return m?Number(m[1]):null;}
async function dayMsg(s,d){
  if(weekday(d)===0)return label(d)+"\n\n月曜日のためTOSS監視対象外です。\n\n🔗 TOSSを開く：\n"+TOSS_URL;
  const ls=await lineState(),by={9:[],13:[],17:[]};
  for(const[k,v]of Object.entries(s.statuses||{})){if(!v||v.status!=="空き")continue;const p=k.split("|");if(p.length<3||p[0]!==d)continue;const h=hour(v.time_name||p[2]);if(by[h])by[h].push({v:v,card:v.card_no==null?p[1]:v.card_no,time:v.time_name||p[2]});}
  for(const h of HOURS)by[h].sort((a,b)=>(Number(a.card)||9999)-(Number(b.card)||9999));
  const lines=[label(d),"最終取得："+fresh(s.saved_at),""];
  for(let i=0;i<HOURS.length;i++){const h=HOURS[i];if(i)lines.push("");lines.push("【"+h+"時】");if(!by[h].length){lines.push("空きなし");continue;}for(const x of by[h]){const f=x.v.facility||"不明";if(!ls.facility_icons[f]){const pal=["🔵","🟢","🟠","🟣","🟡","🔴","🟦","🟩","🟧","🟪","🟨","🟥"],used=new Set(Object.values(ls.facility_icons));ls.facility_icons[f]=pal.find(z=>!used.has(z))||pal[Object.keys(ls.facility_icons).length%pal.length];}lines.push(ls.facility_icons[f]+" "+f);lines.push(x.v.item||"不明");lines.push("");}}
  await saveLine(ls);
  lines.push("🔗 予約サイトはこちら：",TOSS_URL);
  return lines.join("\n").replace(/\n{3,}/g,"\n\n");
}
async function lineApi(path,body){if(typeof LINE_CHANNEL_ACCESS_TOKEN==="undefined"||!LINE_CHANNEL_ACCESS_TOKEN)throw Error("LINE_CHANNEL_ACCESS_TOKENが未設定です。");const r=await fetch("https://api.line.me"+path,{method:"POST",headers:{"Authorization":"Bearer "+LINE_CHANNEL_ACCESS_TOKEN,"Content-Type":"application/json"},body:JSON.stringify(body)});const t=await r.text();if(!r.ok)throw Error("LINE API HTTP "+r.status+": "+t.slice(0,400));return t?JSON.parse(t):{};}
async function reply(t,msgs){if(!t)return;await lineApi("/v2/bot/message/reply",{replyToken:t,messages:msgs.slice(0,5).map(x=>typeof x==="string"?{type:"text",text:x.slice(0,5000)}:x)});}
async function push(msgs,silent){if(typeof LINE_ALLOWED_USER_ID==="undefined"||!LINE_ALLOWED_USER_ID)throw Error("LINE_ALLOWED_USER_IDが未設定です。");await lineApi("/v2/bot/message/push",{to:LINE_ALLOWED_USER_ID,messages:msgs.slice(0,5).map(x=>typeof x==="string"?{type:"text",text:x.slice(0,5000)}:x),notificationDisabled:silent===true});const s=await lineState();s.last_line_send_at=nowIso();s.last_line_error=null;await saveLine(s);}
async function lineError(msg){try{const s=await lineState();s.last_line_error=String(msg).slice(0,1500);await saveLine(s);}catch{}}
function picker(title,data,min,max,step,labelText,color){return{type:"flex",altText:title+"（"+min+"～"+max+"）",contents:{type:"bubble",size:"kilo",header:{type:"box",layout:"horizontal",backgroundColor:color,paddingAll:"16px",contents:[{type:"text",text:step,color:"#FFFFFF",weight:"bold",size:"sm",flex:0},{type:"text",text:labelText,color:"#FFFFFF",weight:"bold",size:"lg",align:"end",flex:1}]},body:{type:"box",layout:"vertical",paddingAll:"20px",contents:[{type:"text",text:title,size:"lg",weight:"bold",wrap:true},{type:"text",text:"選択可能\n"+min+" ～ "+max,size:"sm",wrap:true,margin:"md"}]},footer:{type:"box",layout:"vertical",spacing:"sm",paddingAll:"16px",contents:[{type:"button",style:"primary",color:color,action:{type:"datetimepicker",label:data.indexOf("_end")>=0?"終了日を選択":"開始日を選択",data:data,mode:"date",min:min,max:max}},{type:"button",style:"secondary",action:{type:"postback",label:"← トップメニュー",data:"menu=top"}}]}}};}
async function operation(feature,step,start){const s=await lineState();s.operation={feature:feature,step:step,start_date:start||null,expires_at:new Date(Date.now()+600000).toISOString()};await saveLine(s);}
async function clearOp(s){const x=s||await lineState();x.operation={feature:null,step:null,start_date:null,expires_at:null};return saveLine(x);}
async function datePostback(e){
  const q=e.postback?.params||{},d=String(q.date||q.datetime||"").slice(0,10);if(!dateOk(d)){await reply(e.replyToken,["日付を読み取れませんでした。トップメニューからやり直してください。"]);return;}
  const s=await lineState(),op=s.operation||{},w={today:today()};w.maximum=addDay(w.today,DAY_MAX);
  if(!op.expires_at||Date.now()>=Date.parse(op.expires_at)){await clearOp(s);await reply(e.replyToken,["操作が10分以上経過したため、選択状態を破棄しました。","トップメニューからもう一度選択してください。"]);return;}
  if(op.feature==="availability"&&op.step==="start"){const min=addDay(w.today,1);if(d<min||d>w.maximum){await reply(e.replyToken,["検索開始日は明日～13日後の範囲で選択してください。"]);return;}await operation("availability","end",d);await reply(e.replyToken,[picker("検索終了日を選択してください","flow=availability_end",d,w.maximum,"STEP 2","終了日","#16A34A")]);return;}
  if(op.feature==="availability"&&op.step==="end"){const st=op.start_date;if(!dateOk(st)||d<st||d>w.maximum){await reply(e.replyToken,["検索期間が不正です。"]);return;}const s0=(await tossState()).state,msgs=[];for(let x=st;x<=d;x=addDay(x,1))msgs.push(await dayMsg(s0,x));await reply(e.replyToken,msgs.slice(0,5));for(let i=5;i<msgs.length;i+=5)try{await push(msgs.slice(i,i+5),true)}catch(err){await lineError("検索結果push失敗: "+err)}await clearOp(s);return;}
  if(op.feature==="monitor"&&op.step==="start"){if(d<w.today||d>w.maximum){await reply(e.replyToken,["監視開始日は今日～13日後の範囲で選択してください。"]);return;}await operation("monitor","end",d);await reply(e.replyToken,[picker("監視終了日を選択してください","flow=monitor_end",d,w.maximum,"STEP 2","終了日","#16A34A")]);return;}
  if(op.feature==="monitor"&&op.step==="end"){const st=op.start_date;if(!dateOk(st)||d<st||d>w.maximum){await reply(e.replyToken,["監視期間が不正です。"]);return;}const n=await lineState();n.monitoring={enabled:true,start_date:st,end_date:d,enabled_at:nowIso()};n.last_seen_state_saved_at=null;n.last_toss_alert_signature=null;n.last_toss_runtime_signature=null;n.monitor_stale_notified=false;await setConfig(true);await clearOp(n);await reply(e.replyToken,["監視モードを開始しました。\n期間："+st.replaceAll("-","/")+" ～ "+d.replaceAll("-","/")+"\n\n空き化（○以外→○）を検知したときに通知します。"]);return;}
}
async function postback(e){
  const d=e.postback?.data||"";
  if(d==="menu=top"){await clearOp();await reply(e.replyToken,["トップメニューに戻りました。下のメニューから選択してください。"]);return;}
  if(d==="menu=today"){try{const s=(await tossState()).state;await reply(e.replyToken,[await dayMsg(s,today())]);}catch(err){await lineError("今日の空き取得失敗: "+err);await reply(e.replyToken,["保存済みTOSS状態を読み取れませんでした。\n「現在の状態」でエラーを確認してください。"]);}return;}
  if(d==="menu=availability"){const w={today:today()};w.maximum=addDay(w.today,DAY_MAX);await operation("availability","start");await reply(e.replyToken,[picker("検索開始日を選択してください","flow=availability_start",addDay(w.today,1),w.maximum,"STEP 1","開始日","#2563EB")]);return;}
  if(d==="menu=monitor"){const w={today:today()};w.maximum=addDay(w.today,DAY_MAX);await operation("monitor","start");await reply(e.replyToken,[picker("監視開始日を選択してください","flow=monitor_start",w.today,w.maximum,"STEP 1","開始日","#2563EB")]);return;}
  if(d==="menu=cancel_monitor"){const s=await lineState(),was=!!s.monitoring?.enabled;s.monitoring={enabled:false,start_date:null,end_date:null,enabled_at:null};s.last_seen_state_saved_at=null;s.last_toss_alert_signature=null;s.last_toss_runtime_signature=null;s.monitor_stale_notified=false;await setConfig(false);await clearOp(s);await reply(e.replyToken,[was?"監視モードをキャンセルしました。\n監視は停止しています。\n必要になったら「監視モード」から再開してください。":"監視モードはすでにキャンセルされています。"]);return;}
  if(d==="menu=status"){await clearOp();const r=await runtime(),c=await config(),ls=await lineState();let ts={};let bak=false;try{const z=await tossState();ts=z.state;bak=z.backup}catch{}const lines=["===== 現在の状態 =====",ls.monitoring?.enabled?"監視状況：監視中\n期間："+ls.monitoring.start_date+" ～ "+ls.monitoring.end_date:"監視状況：停止中","TOSS監視："+(c.monitor_enabled?"ON":"OFF"),"","エラー状況："];const errorItems=[];for(const k of ERRORS){const v=k==="LINE送信エラー"?ls.last_line_error:er[k];if(v)errorItems.push("・"+k+"："+(Array.isArray(v)?v.join(" / "):String(v)));}if(bak)errorItems.push("・状態ファイルエラー（TOSS）：本体に問題がありバックアップを使用中");if(!errorItems.length){lines.push("【＝＝＝＝ 現在エラーはありません ＝＝＝＝】");}else{lines.push("【エラーあり】",...errorItems);}lines.push("","メンテナンス情報：");if(r.monitor_status==="maintenance")lines.push("メンテナンス：現在メンテナンス中です。");else{const ds=Array.isArray(ts.maintenance_dates)?ts.maintenance_dates.filter(dateOk):[],w={today:today()};w.maximum=addDay(w.today,DAY_MAX);const sh=ds.filter(x=>x>=w.today&&x<=w.maximum);lines.push(sh.length?"メンテナンス予定："+sh.map(x=>x.replaceAll("-","/")).join(", "):"メンテナンス：現在の告知なし");}lines.push("",r.last_success_at?"TOSS最終正常取得："+fresh(r.last_success_at):"保存済み状態："+fresh(ts.saved_at));await reply(e.replyToken,[lines.join("\n")]);return;}
  if(d.indexOf("flow=")===0){await datePostback(e);}
}
async function verify(body,sig){
  if(typeof LINE_CHANNEL_SECRET==="undefined"||!LINE_CHANNEL_SECRET||!sig)return false;
  const k=await crypto.subtle.importKey("raw",new TextEncoder().encode(LINE_CHANNEL_SECRET),{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  const b=new Uint8Array(await crypto.subtle.sign("HMAC",k,new TextEncoder().encode(body)));let bin="";for(const x of b)bin+=String.fromCharCode(x);return btoa(bin)===sig;
}
async function webhook(data){
  const s=await lineState(),seen=new Set(s.processed_event_ids||[]);
  for(const e of data.events||[]){const id=e.webhookEventId;if(id&&seen.has(id))continue;const uid=e.source?.userId;if(typeof LINE_ALLOWED_USER_ID==="undefined"||!LINE_ALLOWED_USER_ID||uid!==LINE_ALLOWED_USER_ID){if(id)seen.add(id);continue;}try{if(e.type==="postback")await postback(e)}catch(err){await lineError("Webhook処理失敗: "+err)}if(id)seen.add(id);}
  const n=await lineState();n.processed_event_ids=Array.from(seen).slice(-200);await saveLine(n);
}
async function scheduledTask(){
  const c=await config();if(!c.monitor_enabled)return;let ls=await lineState(),r=await runtime();
  if(!ls.monitoring?.enabled)return;
  const en=Date.parse(ls.monitoring.enabled_at||""),ru=Date.parse(r.updated_at||"");
  const er=r.errors&&typeof r.errors==="object"?r.errors:{};const payload={};for(const k of ERRORS)if(k!=="LINE送信エラー"&&er[k])payload[k]=er[k];const es=JSON.stringify(payload);
  if(Number.isFinite(en)&&Number.isFinite(ru)&&ru>=en){if(es&&es!==ls.last_toss_runtime_signature){if(ls.last_toss_runtime_signature!==null)try{await push(["TOSS監視でエラーを検知しました。",es],false)}catch(err){await lineError("TOSSエラー通知失敗: "+err)}ls.last_toss_runtime_signature=es}else if(!es&&ls.last_toss_runtime_signature){try{await push(["TOSS監視が正常状態に復旧しました。"],false)}catch(err){await lineError("復旧通知失敗: "+err)}ls.last_toss_runtime_signature=null;}}
  try{const cur=(await tossState()).state,saved=String(cur.saved_at||""),st=dateOk(ls.monitoring.start_date)?ls.monitoring.start_date:null,ed=dateOk(ls.monitoring.end_date)?ls.monitoring.end_date:null;
    if(saved&&st&&ed){if(today()>ed){try{await push(["監視期間が終了しました。\n期間："+st.replaceAll("-","/")+" ～ "+ed.replaceAll("-","/")],false)}catch(err){await lineError("監視終了通知失敗: "+err)}ls.monitoring={enabled:false,start_date:null,end_date:null,enabled_at:null};ls.last_seen_state_saved_at=null;await setConfig(false);}
    else if(!ls.last_seen_state_saved_at){ls.last_seen_state_saved_at=saved;}
    else if(saved!==ls.last_seen_state_saved_at&&(!ls.monitoring.enabled_at||Date.parse(saved)>=Date.parse(ls.monitoring.enabled_at))){let prev={statuses:{}};try{prev=await read(STATE_BAK_KEY,{statuses:{}})}catch{}const changes=[];
      for(const[k,v]of Object.entries(cur.statuses||{})){if(!v||v.status!=="空き")continue;const p=k.split("|"),dd=p[0],d2=dateOk(dd)?dd:null;if(!d2||d2<st||d2>ed)continue;const old=prev.statuses?.[k];if(!old||old.status==="空き")continue;changes.push({date:dd,facility:v.facility||"不明",item:v.item||"不明",time_name:v.time_name||p[2],card_no:v.card_no==null?p[1]:v.card_no,from:old.status});}
      changes.sort((a,b)=>[a.date,hour(a.time_name)??99,Number(a.card_no)||9999,a.time_name].join("|").localeCompare([b.date,hour(b.time_name)??99,Number(b.card_no)||9999,b.time_name].join("|")));
      if(changes.length){const sig=btoa(unescape(encodeURIComponent(JSON.stringify(changes))));if(sig!==ls.last_toss_alert_signature){try{await push(["===== 空きが出ました =====","最終取得："+fresh(cur.saved_at),"",...changes.map(x=>"■ "+x.date.replaceAll("-","/")+"\n"+x.facility+" / "+x.item+" / "+x.time_name+"\n前回："+x.from+" → ○\n"+TOSS_URL)],false);ls.last_toss_alert_signature=sig}catch(err){await lineError("空き化通知失敗: "+err)}}}
      else ls.last_toss_alert_signature=null;ls.last_seen_state_saved_at=saved;}
    }
  }catch(err){await lineError("監視状態比較失敗: "+err);}
  if(r.monitor_status!=="maintenance"){const last=r.last_scan_started_at||r.updated_at,ts=Date.parse(last||"");if(Number.isFinite(ts)&&(!Number.isFinite(en)||ts>=en)){const age=Date.now()-ts;if(age>STALE_MS&&!ls.monitor_stale_notified){try{await push(["TOSS監視停止の可能性があります。\n最終監視開始："+fresh(last)+"\n現在の状態から詳細を確認してください。"],false)}catch(err){await lineError("監視停止疑い通知失敗: "+err)}ls.monitor_stale_notified=true}else if(age<=STALE_MS&&ls.monitor_stale_notified){try{await push(["TOSS監視の動作を確認しました。監視を再開しています。"],false)}catch(err){await lineError("監視復帰通知失敗: "+err)}ls.monitor_stale_notified=false;}}}
  await saveLine(ls);
}
addEventListener("fetch",event=>event.respondWith((async()=>{const u=new URL(event.request.url);if(event.request.method==="GET"&&u.pathname==="/")return json({ok:true,service:"toss-line-bot"});if(event.request.method==="GET"&&u.pathname==="/health")return json({ok:true,service:"toss-line-bot",kv_bound:typeof TOSS_KV!=="undefined",line_secret_configured:typeof LINE_CHANNEL_SECRET!=="undefined"&&!!LINE_CHANNEL_SECRET,line_access_token_configured:typeof LINE_CHANNEL_ACCESS_TOKEN!=="undefined"&&!!LINE_CHANNEL_ACCESS_TOKEN,allowed_user_configured:typeof LINE_ALLOWED_USER_ID!=="undefined"&&!!LINE_ALLOWED_USER_ID});if(event.request.method!=="POST"||u.pathname!=="/callback")return new Response("Not Found",{status:404});const len=Number(event.request.headers.get("content-length")||0);if(len<=0||len>MAX_BODY)return new Response("Payload Too Large",{status:413});const body=await event.request.text();if(new TextEncoder().encode(body).length>MAX_BODY||!(await verify(body,event.request.headers.get("x-line-signature")||"")))return new Response("Bad Request",{status:400});let data;try{data=JSON.parse(body)}catch{return new Response("Bad Request",{status:400})}if(!Array.isArray(data.events))return new Response("Bad Request",{status:400});if(!data.events.length)return new Response("OK");event.waitUntil(webhook(data));return new Response("OK")})()));
addEventListener("scheduled",event=>event.waitUntil(scheduledTask()));