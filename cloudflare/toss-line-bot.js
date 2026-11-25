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
const STALE_MS = 1200000;
const NIGHT_PAUSE_START_HOUR = 1;
const NIGHT_PAUSE_END_HOUR = 7;
const GITHUB_REPO = "11kai21/toss-monitor";
const PRODUCTION_WORKFLOW = "toss-monitor-production.yml";
const RESTART_KEY = "toss_restart_state";
const RECOVERY_MS = 720000;
// LINE DevelopersのLIFFアプリID（公開してよい値）。空の間は従来の日付ダイヤルだけを使う。
const LIFF_ID = "2012795970-x6AbQgoh";

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
// 表示用の日付（例：2026/11/25（水））。日付として読めない値はそのまま返す。
function ymdw(v){return dateOk(v)?v.replaceAll("-","/")+"（"+WD[weekday(v)]+"）":String(v);}
function fresh(v){if(!v)return"取得時刻不明";const d=new Date(v.indexOf("T")>=0?v:v.replace(" ","T")+"+09:00");if(Number.isNaN(d.getTime()))return"取得時刻不明";const m=Math.max(0,Math.floor((Date.now()-d.getTime())/60000));if(m<1)return"1分未満前";if(m<60)return m+"分前";const h=Math.floor(m/60),r=m%60;return h+"時間"+(r?r+"分":"")+"前";}
async function read(key,fallback){const v=await TOSS_KV.get(key);if(v===null)return fallback;return JSON.parse(v);}
async function tossState(){let s=null,b=false;try{s=await read(STATE_KEY,null)}catch{}if(!s?.statuses){try{s=await read(STATE_BAK_KEY,null);b=true}catch{}}if(!s?.statuses)throw Error("TOSS状態を読み取れません。");return{state:s,backup:b};}
function baseLine(){return{version:1,updated_at:nowIso(),monitoring:{enabled:false,start_date:null,end_date:null,enabled_at:null},operation:{feature:null,step:null,start_date:null,expires_at:null},facility_icons:{},processed_event_ids:[],last_line_error:null,last_line_send_at:null,last_seen_state_saved_at:null,last_toss_alert_signature:null,last_toss_runtime_signature:null,monitor_stale_notified:false,night_baseline_date:null};}
async function lineState(){let s=null;try{s=await read(LINE_STATE_KEY,null)}catch{}if(!s){try{s=await read(LINE_STATE_BAK_KEY,null)}catch{}}const b=baseLine();if(!s||typeof s!=="object")return b;return{...b,...s,monitoring:{...b.monitoring,...(s.monitoring||{})},operation:{...b.operation,...(s.operation||{})},facility_icons:s.facility_icons&&typeof s.facility_icons==="object"?s.facility_icons:{},processed_event_ids:Array.isArray(s.processed_event_ids)?s.processed_event_ids.slice(-200):[]};}
async function saveLine(s){const old=await TOSS_KV.get(LINE_STATE_KEY);if(old!==null)await TOSS_KV.put(LINE_STATE_BAK_KEY,old);const n={...s,updated_at:nowIso()};await TOSS_KV.put(LINE_STATE_KEY,JSON.stringify(n));return n;}
async function runtime(){try{const s=await read(RUNTIME_KEY,{});return s&&typeof s==="object"?s:{}}catch{return{}}}
async function config(){try{const s=await read(CONFIG_KEY,{monitor_enabled:false});return{monitor_enabled:s.monitor_enabled===true,updated_at:s.updated_at||null}}catch{return{monitor_enabled:false}}}
async function setConfig(on){await TOSS_KV.put(CONFIG_KEY,JSON.stringify({monitor_enabled:on===true,updated_at:nowIso()}));}
function hour(v){const m=String(v||"").match(/(?:^|\D)(\d{1,2})(?=\s*(?:時|:|：))/);return m?Number(m[1]):null;}
async function dayMsg(s,d){
  if(weekday(d)===0)return label(d)+"\n\n月曜日のためTOSS監視対象外です。\n\n🔗 TOSSを開く：\n"+TOSS_URL;
  const ls=await lineState(),beforeIcons=JSON.stringify(ls.facility_icons||{}),by={9:[],13:[],17:[]};
  for(const[k,v]of Object.entries(s.statuses||{})){if(!v||v.status!=="空き")continue;const p=k.split("|");if(p.length<3||p[0]!==d)continue;const h=hour(v.time_name||p[2]);if(by[h])by[h].push({v:v,card:v.card_no==null?p[1]:v.card_no,time:v.time_name||p[2]});}
  for(const h of HOURS)by[h].sort((a,b)=>(Number(a.card)||9999)-(Number(b.card)||9999));
  const r=await runtime(),displaySavedAt=r.last_success_at||s.saved_at;
  const lines=[label(d),"最終取得："+fresh(displaySavedAt),""];
  for(let i=0;i<HOURS.length;i++){const h=HOURS[i];if(i)lines.push("");lines.push("【"+h+"時】");if(!by[h].length){lines.push("空きなし");continue;}for(const x of by[h]){const f=x.v.facility||"不明";if(!ls.facility_icons[f]){const pal=["🔵","🟢","🟠","🟣","🟡","🔴","🟦","🟩","🟧","🟪","🟨","🟥"],used=new Set(Object.values(ls.facility_icons));ls.facility_icons[f]=pal.find(z=>!used.has(z))||pal[Object.keys(ls.facility_icons).length%pal.length];}lines.push(ls.facility_icons[f]+" "+f);lines.push(x.v.item||"不明");lines.push("");}}
  if(JSON.stringify(ls.facility_icons||{})!==beforeIcons)await saveLine(ls);
  lines.push("🔗 予約サイトはこちら：",TOSS_URL);
  return lines.join("\n").replace(/\n{3,}/g,"\n\n");
}
async function lineApi(path,body){if(typeof LINE_CHANNEL_ACCESS_TOKEN==="undefined"||!LINE_CHANNEL_ACCESS_TOKEN)throw Error("LINE_CHANNEL_ACCESS_TOKENが未設定です。");const r=await fetch("https://api.line.me"+path,{method:"POST",headers:{"Authorization":"Bearer "+LINE_CHANNEL_ACCESS_TOKEN,"Content-Type":"application/json"},body:JSON.stringify(body)});const t=await r.text();if(!r.ok)throw Error("LINE API HTTP "+r.status+": "+t.slice(0,400));return t?JSON.parse(t):{};}
async function reply(t,msgs){if(!t)return;await lineApi("/v2/bot/message/reply",{replyToken:t,messages:msgs.slice(0,5).map(x=>typeof x==="string"?{type:"text",text:x.slice(0,5000)}:x)});}
async function push(msgs,silent){if(typeof LINE_ALLOWED_USER_ID==="undefined"||!LINE_ALLOWED_USER_ID)throw Error("LINE_ALLOWED_USER_IDが未設定です。");await lineApi("/v2/bot/message/push",{to:LINE_ALLOWED_USER_ID,messages:msgs.filter(x=>x!=="").slice(0,5).map(x=>typeof x==="string"?{type:"text",text:x.slice(0,5000)}:x),notificationDisabled:silent===true});const s=await lineState();s.last_line_send_at=nowIso();s.last_line_error=null;await saveLine(s);}
async function lineError(msg){try{const s=await lineState();s.last_line_error=String(msg).slice(0,1500);await saveLine(s);}catch{}}
// 定期処理用の送信。送信結果は最後に保存するlsへ書き、エラー記録が上書きで消えないようにする。
// 失敗時はKVへ直接書かない（同じエラーの再試行が続いてもKV書き込みが増えないようにするため）。
async function notify(ls,msgs,errLabel){try{for(let i=0;i<msgs.length;i+=5)await push(msgs.slice(i,i+5),false);ls.last_line_send_at=nowIso();ls.last_line_error=null;return true}catch(err){ls.last_line_error=String(errLabel+": "+err).slice(0,1500);return false}}
// 空き化通知は空文字のメッセージを作らず1通にまとめ、5000文字を超える場合だけ枠の区切りで分割する。
function vacancyMessages(state,changes){const head="===== 空きが出ました =====\n最終取得："+fresh(state.saved_at),out=[];let cur=head;for(const x of changes){const b="■ "+ymdw(x.date)+"\n"+x.facility+" / "+x.item+" / "+x.time_name+"\n前回："+x.from+" → ○\n"+TOSS_URL;if((cur+"\n\n"+b).length>5000){out.push(cur);cur=b;}else cur+="\n\n"+b;}out.push(cur);return out;}
function picker(title,data,min,max,step,labelText,color){return{type:"flex",altText:title+"（"+min+"～"+max+"）",contents:{type:"bubble",size:"kilo",header:{type:"box",layout:"horizontal",backgroundColor:color,paddingAll:"16px",contents:[{type:"text",text:step,color:"#FFFFFF",weight:"bold",size:"sm",flex:0},{type:"text",text:labelText,color:"#FFFFFF",weight:"bold",size:"lg",align:"end",flex:1}]},body:{type:"box",layout:"vertical",paddingAll:"20px",contents:[{type:"text",text:title,size:"lg",weight:"bold",wrap:true},{type:"text",text:"選択可能\n"+ymdw(min)+" ～ "+ymdw(max),size:"sm",wrap:true,margin:"md"}]},footer:{type:"box",layout:"vertical",spacing:"sm",paddingAll:"16px",contents:[{type:"button",style:"primary",color:color,action:{type:"datetimepicker",label:data.indexOf("_end")>=0?"終了日を選択":"開始日を選択",data:data,mode:"date",min:min,max:max}},{type:"button",style:"secondary",action:{type:"postback",label:"← トップメニュー",data:"menu=top"}}]}}};}
// ===== カレンダー（LIFF）で期間を選ぶ =====
// LIFF画面で開始日・終了日を選ぶと、本人のトークから「📅 監視モード 2026/11/27〜2026/12/03」が送られ、
// 下のtextMessageが受け取って従来の日付ダイヤルと同じ処理を行う。送信者は従来どおりLINE_ALLOWED_USER_IDだけを受け付ける。
// LIFF_IDが空の間はカレンダーのボタンを出さず、従来の日付ダイヤルだけを使う。
function calendarCard(mode,min,max){const title=mode==="monitor"?"監視期間を選択してください":"検索期間を選択してください",dial=mode==="monitor"?"flow=monitor_start":"flow=availability_start";return{type:"flex",altText:title+"（"+min+"～"+max+"）",contents:{type:"bubble",size:"kilo",header:{type:"box",layout:"horizontal",backgroundColor:"#185FA5",paddingAll:"16px",contents:[{type:"text",text:mode==="monitor"?"監視モード":"明日以降の空き",color:"#FFFFFF",weight:"bold",size:"sm",flex:0},{type:"text",text:"期間を選択",color:"#FFFFFF",weight:"bold",size:"lg",align:"end",flex:1}]},body:{type:"box",layout:"vertical",paddingAll:"20px",contents:[{type:"text",text:title,size:"lg",weight:"bold",wrap:true},{type:"text",text:"選択可能\n"+ymdw(min)+" ～ "+ymdw(max),size:"sm",wrap:true,margin:"md"}]},footer:{type:"box",layout:"vertical",spacing:"sm",paddingAll:"16px",contents:[{type:"button",style:"primary",color:"#185FA5",action:{type:"uri",label:"カレンダーで選ぶ",uri:"https://liff.line.me/"+LIFF_ID+"?mode="+mode}},{type:"button",style:"secondary",action:{type:"datetimepicker",label:"日付ダイヤルで選ぶ",data:dial,mode:"date",min:min,max:max}},{type:"button",style:"secondary",action:{type:"postback",label:"← トップメニュー",data:"menu=top"}}]}}};}
function rangeOk(mode,st,ed){const t=today(),max=addDay(t,DAY_MAX),min=mode==="monitor"?t:addDay(t,1);return dateOk(st)&&dateOk(ed)&&st>=min&&ed<=max&&st<=ed;}
async function startMonitor(token,st,d){const n=await lineState();n.monitoring={enabled:true,start_date:st,end_date:d,enabled_at:nowIso()};n.last_seen_state_saved_at=null;n.last_toss_alert_signature=null;n.last_toss_runtime_signature=null;n.monitor_stale_notified=false;n.operation={feature:null,step:null,start_date:null,expires_at:null};await setConfig(true);await saveLine(n);await reply(token,["監視モードを開始しました。\n期間："+ymdw(st)+" ～ "+ymdw(d)+"\n\n空き化（○以外→○）を検知したときに通知します。"]);}
async function searchAvailability(token,st,d){const s0=(await tossState()).state,msgs=[];for(let x=st;x<=d;x=addDay(x,1))msgs.push(await dayMsg(s0,x));await reply(token,msgs.slice(0,5));for(let i=5;i<msgs.length;i+=5)try{await push(msgs.slice(i,i+5),true)}catch(err){await lineError("検索結果push失敗: "+err)}await clearOp();}
async function textMessage(e){
  const text=String(e.message?.text||"").trim();
  if(text===RM_ON||text===RM_OFF){await richMenuSwitch(e.replyToken,text===RM_ON);return;}
  const dial=text.match(/^📅\s*日付ダイヤル\s*(監視モード|空き検索)$/);
  if(dial){const t=today(),max=addDay(t,DAY_MAX);if(dial[1]==="監視モード"){await operation("monitor","start");await reply(e.replyToken,[picker("監視開始日を選択してください","flow=monitor_start",t,max,"STEP 1","開始日","#2563EB")]);}else{await operation("availability","start");await reply(e.replyToken,[picker("検索開始日を選択してください","flow=availability_start",addDay(t,1),max,"STEP 1","開始日","#2563EB")]);}return;}
  const m=text.match(/^📅\s*(監視モード|空き検索)\s*(\d{4})\/(\d{2})\/(\d{2})(?:[（(].[）)])?\s*[〜～~]\s*(\d{4})\/(\d{2})\/(\d{2})(?:[（(].[）)])?$/);if(!m)return;
  const mode=m[1]==="監視モード"?"monitor":"availability",st=m[2]+"-"+m[3]+"-"+m[4],ed=m[5]+"-"+m[6]+"-"+m[7];
  if(!rangeOk(mode,st,ed)){await reply(e.replyToken,["選べる範囲外の期間です。トップメニューからもう一度カレンダーで選んでください。"]);return;}
  if(mode==="monitor")await startMonitor(e.replyToken,st,ed);else await searchAvailability(e.replyToken,st,ed);
}
// ===== リッチメニューの「監視モード」「明日以降の空き」をカレンダー直結に切り替える =====
// 本人がトークで RM_ON を送ると、今のリッチメニューを複製し、2つのボタンだけLIFFのURLを開く動作に変えて差し替える。
// 文言・配置・画像・他のボタンはそのまま。元のメニューは消さずに残し、RM_OFF で元に戻す。
const RM_ON = "#リッチメニュー カレンダー直結", RM_OFF = "#リッチメニュー 元に戻す", RM_KEY = "toss_richmenu_switch";
async function lineReq(url,opt){const o=opt||{},h={"Authorization":"Bearer "+LINE_CHANNEL_ACCESS_TOKEN,...(o.headers||{})};const r=await fetch(url,{method:o.method||"GET",headers:h,body:o.body});if(o.allow404&&r.status===404)return null;if(!r.ok)throw Error("LINE API HTTP "+r.status+": "+(await r.text()).slice(0,300));return r;}
async function currentRichMenu(){const uid=LINE_ALLOWED_USER_ID;let r=await lineReq("https://api.line.me/v2/bot/user/"+uid+"/richmenu",{allow404:true});if(r)return{id:(await r.json()).richMenuId,perUser:true};r=await lineReq("https://api.line.me/v2/bot/user/all/richmenu",{allow404:true});if(r)return{id:(await r.json()).richMenuId,perUser:false};return null;}
async function linkRichMenu(id,perUser){if(perUser)await lineReq("https://api.line.me/v2/bot/user/"+LINE_ALLOWED_USER_ID+"/richmenu/"+id,{method:"POST"});else await lineReq("https://api.line.me/v2/bot/user/all/richmenu/"+id,{method:"POST"});}
async function richMenuSwitch(token,on){
  try{
    const cur=await currentRichMenu();if(!cur){await reply(token,["APIで設定されたリッチメニューが見つかりませんでした。何も変更していません。"]);return;}
    if(!on){const sv=await read(RM_KEY,null);if(!sv?.previous){await reply(token,["元に戻す記録がありません。何も変更していません。"]);return;}await linkRichMenu(sv.previous,sv.perUser===true);await TOSS_KV.put(RM_KEY,JSON.stringify({...sv,active:false,restored_at:nowIso()}));await reply(token,["リッチメニューを元に戻しました。\n「監視モード」「明日以降の空き」は、押すとカードが出る動作に戻ります。"]);return;}
    const menu=await (await lineReq("https://api.line.me/v2/bot/richmenu/"+cur.id)).json();let n=0;
    const areas=(menu.areas||[]).map(a=>{const d=a.action?.type==="postback"?a.action.data:null;if(d!=="menu=monitor"&&d!=="menu=availability")return a;n++;const act={type:"uri",uri:"https://liff.line.me/"+LIFF_ID+"?mode="+(d==="menu=monitor"?"monitor":"availability")};if(a.action.label)act.label=a.action.label;return{bounds:a.bounds,action:act};});
    if(!n){await reply(token,["切り替え対象のボタンが見つかりませんでした（すでにカレンダー直結の可能性があります）。何も変更していません。"]);return;}
    const img=await lineReq("https://api-data.line.me/v2/bot/richmenu/"+cur.id+"/content"),type=img.headers.get("content-type")||"image/png",buf=await img.arrayBuffer();
    const body={size:menu.size,selected:menu.selected,name:String(menu.name||"TOSS").slice(0,280)+" calendar",chatBarText:menu.chatBarText,areas};
    const created=await (await lineReq("https://api.line.me/v2/bot/richmenu",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json(),nid=created.richMenuId;
    try{await lineReq("https://api-data.line.me/v2/bot/richmenu/"+nid+"/content",{method:"POST",headers:{"Content-Type":type},body:buf});await linkRichMenu(nid,cur.perUser);}
    catch(err){try{await lineReq("https://api.line.me/v2/bot/richmenu/"+nid,{method:"DELETE"})}catch{}throw err;}
    await TOSS_KV.put(RM_KEY,JSON.stringify({previous:cur.id,current:nid,perUser:cur.perUser,active:true,switched_at:nowIso()}));
    await reply(token,["リッチメニューを切り替えました。\n「監視モード」「明日以降の空き」を押すと、すぐカレンダーが開きます。\n（反映まで少し時間がかかったり、トークを開き直す必要がある場合があります）\n\n元に戻すときは「"+RM_OFF+"」と送ってください。"]);
  }catch(err){await lineError("リッチメニュー切替失敗: "+err);await reply(token,["リッチメニューの切り替えに失敗しました。元のメニューのままです。\n"+String(err).slice(0,300)]);}
}
function calendarPage(){const t=today();return new Response(CAL_HTML.replace("__TODAY__",t).replace("__MAX__",addDay(t,DAY_MAX)).replace("__LIFF__",LIFF_ID),{headers:{"content-type":"text/html; charset=utf-8","cache-control":"no-store"}});}
const CAL_HTML = String.raw`<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>期間を選択</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;background:#F3F4F6;color:#111827}
.hd{background:#185FA5;color:#fff;padding:14px 16px;display:flex;justify-content:space-between;align-items:center}
.hd b{font-size:15px}.hd span{font-size:16px;font-weight:600}
.wrap{padding:14px 12px}
.mo{font-size:14px;color:#4B5563;margin:0 4px 8px;font-weight:600}
.wd,.gr{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}
.wd div{text-align:center;font-size:12px;color:#6B7280;padding-bottom:4px}
.c{height:48px;border-radius:9px;border:1px solid #E5E7EB;background:#fff;font-size:16px;display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.1;color:#111827;padding:0;-webkit-tap-highlight-color:transparent}
.c small{font-size:10px}
.x{background:#E5E7EB;color:#9CA3AF;border-color:transparent}
.in{background:#E6F1FB;color:#0C447C;border-color:#85B7EB}
.on{background:#185FA5;color:#fff;border-color:#185FA5}
.m1{font-weight:700}
.lg{display:flex;gap:14px;font-size:12px;color:#6B7280;margin:12px 4px 0;flex-wrap:wrap}
.lg i{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:-1px;margin-right:4px}
.sum{margin:12px 4px 0;font-size:14px;color:#374151;min-height:20px}
.err{color:#B91C1C;font-size:13px;margin:8px 4px 0}
.ft{display:flex;gap:10px;padding:4px 12px 16px}
.ft button{flex:1;height:46px;border-radius:10px;font-size:15px;border:1px solid #D1D5DB;background:#fff;color:#111827}
.ft .ok{background:#06C755;border-color:#06C755;color:#fff;font-weight:600}
.ft .ok:disabled{background:#A7F3D0;border-color:#A7F3D0}
.alt{text-align:center;padding:0 12px 18px}
.alt button{background:none;border:none;color:#185FA5;font-size:14px;text-decoration:underline;padding:6px}
</style></head><body>
<div class="hd"><b id="ttl">監視モード</b><span id="step">開始日をタップ</span></div>
<div class="wrap"><div class="mo" id="mo"></div>
<div class="wd"><div>月</div><div>火</div><div>水</div><div>木</div><div>金</div><div>土</div><div>日</div></div>
<div class="gr" id="gr"></div>
<div class="lg"><span><i style="background:#185FA5"></i>選んだ日</span><span><i style="background:#E6F1FB;border:1px solid #85B7EB"></i>期間</span><span><i style="background:#E5E7EB"></i>選べない日・休館</span></div>
<div class="sum" id="sum"></div><div class="err" id="err"></div></div>
<div class="ft"><button type="button" id="rs">選び直す</button><button type="button" class="ok" id="ok" disabled>この期間で決定</button></div>
<div class="alt"><button type="button" id="dial">日付ダイヤルで選ぶ</button></div>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<script>
var TODAY="__TODAY__",MAX="__MAX__",LIFF_ID="__LIFF__",WDS="月火水木金土日";
function q(n){var p=new URLSearchParams(location.search),v=p.get(n);if(v)return v;var s=p.get("liff.state");if(s){try{return new URLSearchParams(s.replace(/^[^?]*\?/,"")).get(n)}catch(x){}}return null}
var mode=q("mode")==="availability"?"availability":"monitor";
function add(v,n){var a=v.split("-").map(Number);return new Date(Date.UTC(a[0],a[1]-1,a[2])+n*864e5).toISOString().slice(0,10)}
function wd(v){var a=v.split("-").map(Number);return (new Date(Date.UTC(a[0],a[1]-1,a[2])).getUTCDay()+6)%7}
function md(v){return Number(v.slice(5,7))+"/"+Number(v.slice(8))}
var MIN=mode==="monitor"?TODAY:add(TODAY,1),s=null,e=null;
document.getElementById("ttl").textContent=mode==="monitor"?"監視モード":"明日以降の空き";
function draw(){var g=document.getElementById("gr");g.innerHTML="";var start=add(MIN,-wd(MIN)),end=add(MAX,6-wd(MAX)),m1=Number(start.slice(5,7)),m2=Number(end.slice(5,7));
 document.getElementById("mo").textContent=start.slice(0,4)+"年 "+m1+"月"+(m1!==m2?"〜"+m2+"月":"");
 for(var d=start;d<=end;d=add(d,1)){(function(d){var b=document.createElement("button");b.type="button";b.className="c";var mon=wd(d)===0,inR=d>=MIN&&d<=MAX,ok=inR&&!mon&&!(s&&!e&&d<s),day=Number(d.slice(8));
  b.innerHTML=(day===1?Number(d.slice(5,7))+"/1":String(day))+(mon&&inR?"<small>休館</small>":"");if(day===1)b.classList.add("m1");
  if(d===s||d===e)b.classList.add("on");else if(s&&e&&d>s&&d<e&&!mon)b.classList.add("in");else if(!ok)b.classList.add("x");
  if(ok)b.onclick=function(){if(!s||e){s=d;e=null}else{e=d}document.getElementById("err").textContent="";draw()};g.appendChild(b)})(d)}
 var st=document.getElementById("step"),sm=document.getElementById("sum");
 if(!s){st.textContent="開始日をタップ";sm.textContent="選べる期間："+md(MIN)+"〜"+md(MAX)}
 else if(!e){st.textContent="終了日をタップ";sm.textContent="開始日 "+md(s)+"（1日だけなら同じ日をもう一度）"}
 else{st.textContent="決定を押してください";sm.textContent="期間："+md(s)+"（"+WDS[wd(s)]+"）〜 "+md(e)+"（"+WDS[wd(e)]+"）"}
 document.getElementById("ok").disabled=!(s&&e)}
document.getElementById("rs").onclick=function(){s=null;e=null;document.getElementById("err").textContent="";draw()};
document.getElementById("ok").onclick=function(){var er=document.getElementById("err"),btn=this;if(!(s&&e)){er.textContent="開始日と終了日を選んでください";return}
 if(!window.liff||!liff.isInClient()){er.textContent="LINEアプリのトーク画面から開いてください";return}
 var t="📅 "+(mode==="monitor"?"監視モード":"空き検索")+" "+s.replace(/-/g,"/")+"（"+WDS[wd(s)]+"）〜"+e.replace(/-/g,"/")+"（"+WDS[wd(e)]+"）";btn.disabled=true;
 liff.sendMessages([{type:"text",text:t}]).then(function(){liff.closeWindow()}).catch(function(x){er.textContent="送信できませんでした："+(x&&x.message||x);btn.disabled=false})};
document.getElementById("dial").onclick=function(){var er=document.getElementById("err"),btn=this;if(!window.liff||!liff.isInClient()){er.textContent="LINEアプリのトーク画面から開いてください";return}
 btn.disabled=true;liff.sendMessages([{type:"text",text:"📅 日付ダイヤル "+(mode==="monitor"?"監視モード":"空き検索")}]).then(function(){liff.closeWindow()}).catch(function(x){er.textContent="送信できませんでした："+(x&&x.message||x);btn.disabled=false})};
draw();
if(window.liff&&LIFF_ID)liff.init({liffId:LIFF_ID}).catch(function(x){document.getElementById("err").textContent="LINEとの接続に失敗しました："+(x&&x.message||x)});
</script></body></html>`;
async function operation(feature,step,start){const s=await lineState();s.operation={feature:feature,step:step,start_date:start||null,expires_at:new Date(Date.now()+600000).toISOString()};await saveLine(s);}
async function clearOp(s){const x=s||await lineState(),before=JSON.stringify(x.operation||{}),empty={feature:null,step:null,start_date:null,expires_at:null};if(before===JSON.stringify(empty))return x;x.operation=empty;return saveLine(x);}
async function datePostback(e){
  const q=e.postback?.params||{},d=String(q.date||q.datetime||"").slice(0,10);if(!dateOk(d)){await reply(e.replyToken,["日付を読み取れませんでした。トップメニューからやり直してください。"]);return;}
  const s=await lineState(),op=s.operation||{},w={today:today()};w.maximum=addDay(w.today,DAY_MAX);
  if(!op.expires_at||Date.now()>=Date.parse(op.expires_at)){await clearOp(s);await reply(e.replyToken,["操作が10分以上経過したため、選択状態を破棄しました。","トップメニューからもう一度選択してください。"]);return;}
  if(op.feature==="availability"&&op.step==="start"){const min=addDay(w.today,1);if(d<min||d>w.maximum){await reply(e.replyToken,["検索開始日は明日～13日後の範囲で選択してください。"]);return;}await operation("availability","end",d);await reply(e.replyToken,[picker("検索終了日を選択してください","flow=availability_end",d,w.maximum,"STEP 2","終了日","#16A34A")]);return;}
  if(op.feature==="availability"&&op.step==="end"){const st=op.start_date;if(!dateOk(st)||d<st||d>w.maximum){await reply(e.replyToken,["検索期間が不正です。"]);return;}await searchAvailability(e.replyToken,st,d);return;}
  if(op.feature==="monitor"&&op.step==="start"){if(d<w.today||d>w.maximum){await reply(e.replyToken,["監視開始日は今日～13日後の範囲で選択してください。"]);return;}await operation("monitor","end",d);await reply(e.replyToken,[picker("監視終了日を選択してください","flow=monitor_end",d,w.maximum,"STEP 2","終了日","#16A34A")]);return;}
  if(op.feature==="monitor"&&op.step==="end"){const st=op.start_date;if(!dateOk(st)||d<st||d>w.maximum){await reply(e.replyToken,["監視期間が不正です。"]);return;}await startMonitor(e.replyToken,st,d);return;}
}
async function postback(e){
  const d=e.postback?.data||"";
  if(d==="menu=top"){await clearOp();await reply(e.replyToken,["トップメニューに戻りました。下のメニューから選択してください。"]);return;}
  if(d==="menu=today"){try{const s=(await tossState()).state;await reply(e.replyToken,[await dayMsg(s,today())]);}catch(err){await lineError("今日の空き取得失敗: "+err);await reply(e.replyToken,["保存済みTOSS状態を読み取れませんでした。\n「現在の状態」でエラーを確認してください。"]);}return;}
  if(d==="menu=availability"){const w={today:today()};w.maximum=addDay(w.today,DAY_MAX);await operation("availability","start");await reply(e.replyToken,[LIFF_ID?calendarCard("availability",addDay(w.today,1),w.maximum):picker("検索開始日を選択してください","flow=availability_start",addDay(w.today,1),w.maximum,"STEP 1","開始日","#2563EB")]);return;}
  if(d==="menu=monitor"){const w={today:today()};w.maximum=addDay(w.today,DAY_MAX);await operation("monitor","start");await reply(e.replyToken,[LIFF_ID?calendarCard("monitor",w.today,w.maximum):picker("監視開始日を選択してください","flow=monitor_start",w.today,w.maximum,"STEP 1","開始日","#2563EB")]);return;}
  if(d==="menu=cancel_monitor"){const s=await lineState(),was=!!s.monitoring?.enabled;s.monitoring={enabled:false,start_date:null,end_date:null,enabled_at:null};s.last_seen_state_saved_at=null;s.last_toss_alert_signature=null;s.last_toss_runtime_signature=null;s.monitor_stale_notified=false;await setConfig(false);await clearOp(s);await reply(e.replyToken,[was?"監視モードをキャンセルしました。\n監視は停止しています。\n必要になったら「監視モード」から再開してください。":"監視モードはすでにキャンセルされています。"]);return;}
  if(d==="menu=status"){await clearOp();const r=await runtime(),c=await config(),ls=await lineState();let ts={};let bak=false;try{const z=await tossState();ts=z.state;bak=z.backup}catch{}const lines=["===== 現在の状態 =====",ls.monitoring?.enabled?"監視状況：監視中\n期間："+ymdw(ls.monitoring.start_date)+" ～ "+ymdw(ls.monitoring.end_date):"監視状況：停止中","TOSS監視："+(c.monitor_enabled?"ON":"OFF"),"","エラー状況："];const er=r.errors&&typeof r.errors==="object"?r.errors:{};const errorItems=[];for(const k of ERRORS){const v=k==="LINE送信エラー"?ls.last_line_error:er[k];if(v)errorItems.push("・"+k+"："+(Array.isArray(v)?v.join(" / "):String(v)));}if(bak)errorItems.push("・状態ファイルエラー（TOSS）：本体に問題がありバックアップを使用中");if(!errorItems.length){lines.push("【＝ 現在エラーはありません ＝】");}else{lines.push("【エラーあり】",...errorItems);}lines.push("","メンテナンス情報：");if(r.monitor_status==="maintenance")lines.push("メンテナンス：現在メンテナンス中です。");else{const ds=Array.isArray(ts.maintenance_dates)?ts.maintenance_dates.filter(dateOk):[],w={today:today()};w.maximum=addDay(w.today,DAY_MAX);const sh=ds.filter(x=>x>=w.today&&x<=w.maximum);lines.push(sh.length?"メンテナンス予定："+sh.map(ymdw).join(", "):"メンテナンス：現在の告知なし");}lines.push("",r.last_success_at?"TOSS最終正常取得："+fresh(r.last_success_at):"保存済み状態："+fresh(ts.saved_at));await reply(e.replyToken,[lines.join("\n")]);return;}
  if(d.indexOf("flow=")===0){await datePostback(e);}
}
async function verify(body,sig){
  if(typeof LINE_CHANNEL_SECRET==="undefined"||!LINE_CHANNEL_SECRET||!sig)return false;
  const k=await crypto.subtle.importKey("raw",new TextEncoder().encode(LINE_CHANNEL_SECRET),{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  const b=new Uint8Array(await crypto.subtle.sign("HMAC",k,new TextEncoder().encode(body)));let bin="";for(const x of b)bin+=String.fromCharCode(x);return btoa(bin)===sig;
}
async function webhook(data){
  const s=await lineState(),before=JSON.stringify(s.processed_event_ids||[]),seen=new Set(s.processed_event_ids||[]);
  for(const e of data.events||[]){const id=e.webhookEventId;if(id&&seen.has(id))continue;const uid=e.source?.userId;if(typeof LINE_ALLOWED_USER_ID==="undefined"||!LINE_ALLOWED_USER_ID||uid!==LINE_ALLOWED_USER_ID){if(id)seen.add(id);continue;}try{if(e.type==="postback")await postback(e);else if(e.type==="message"&&e.message?.type==="text")await textMessage(e)}catch(err){await lineError("Webhook処理失敗: "+err)}if(id)seen.add(id);}
  if(JSON.stringify(Array.from(seen).slice(-200))!==before){const n=await lineState();n.processed_event_ids=Array.from(seen).slice(-200);await saveLine(n);}
}
function refreshMonitoringWindow(s,currentDay){
  const m=s.monitoring;
  if(!m?.enabled)return{active:false,ended:false,changed:false};
  const st=dateOk(m.start_date)?m.start_date:null;
  const ed=dateOk(m.end_date)?m.end_date:null;
  if(!st||!ed)return{active:true,ended:false,changed:false};
  if(currentDay>ed)return{active:false,ended:true,changed:false,previous_start:st,previous_end:ed};
  if(st<currentDay){m.start_date=currentDay;return{active:true,ended:false,changed:true,previous_start:st,previous_end:ed};}
  return{active:true,ended:false,changed:false,previous_start:st,previous_end:ed};
}
let githubTokenExpiry=null;
async function githubApi(method,path,body){const r=await fetch("https://api.github.com/repos/"+GITHUB_REPO+path,{method:method,headers:{"Authorization":"Bearer "+GITHUB_DISPATCH_TOKEN,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"toss-line-bot"},body:body?JSON.stringify(body):undefined});githubTokenExpiry=r.headers.get("github-authentication-token-expiration")||githubTokenExpiry;const t=await r.text();if(!r.ok)throw Error("GitHub API HTTP "+r.status+": "+t.slice(0,300));return t?JSON.parse(t):{};}
// GitHub APIの応答ヘッダーにあるトークン有効期限を見て、14・7・3・1・0日前にLINEで1回ずつ知らせる。
// トークンを作り直して期限が変わると、通知の記録もリセットされる。
const TOKEN_NOTICE_DAYS = [14,7,3,1,0];
function parseGithubExpiry(v){const m=String(v||"").trim().match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*(UTC|Z|[+-]\d{2}:?\d{2})?$/);if(!m)return NaN;const tz=!m[3]||m[3]==="UTC"||m[3]==="Z"?"Z":m[3].replace(/^([+-]\d{2})(\d{2})$/,"$1:$2");return Date.parse(m[1]+"T"+m[2]+tz);}
async function tokenExpiryNotice(st){
  const exp=parseGithubExpiry(githubTokenExpiry);if(!Number.isFinite(exp))return;
  st.token_expires_at=new Date(exp).toISOString();
  const left=Math.ceil((exp-Date.now())/86400000),marks=TOKEN_NOTICE_DAYS.filter(m=>left<=m);if(!marks.length)return;
  const key=st.token_expires_at+"|"+Math.min(...marks);if(st.token_notice_sent===key)return;
  const d=new Intl.DateTimeFormat("ja-JP",{timeZone:"Asia/Tokyo",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(exp));
  try{await push(["GitHubトークン（GITHUB_DISPATCH_TOKEN）の有効期限が近づいています。\n期限："+d+"（あと"+Math.max(0,left)+"日）\n\n期限が切れると、朝の再開と停止時の復旧起動ができなくなります。GitHubでトークンを再生成し、Cloudflareのシークレットを更新してください。"],false);st.token_notice_sent=key;}
  catch(err){await lineError("トークン期限通知失敗: "+err)}
}
// GitHubの時刻指定実行は1時間以上遅れることがあるため、Cloudflare Cronから本番の朝の再開・停止時の復旧起動を行う。
// 判定条件はWatchdogと同じ（夜間停止・メンテナンス中は起動しない、最終正常取得から12分以上、本番Runが動いていない）。
// GITHUB_DISPATCH_TOKENが未設定の間は何もしない。KVへは起動した時とエラー内容が変わった時だけ書く。
async function restartTask(){
  if(typeof GITHUB_DISPATCH_TOKEN==="undefined"||!GITHUB_DISPATCH_TOKEN)return;
  const day=today(),jstParts=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Tokyo",hour:"2-digit",hourCycle:"h23"}).formatToParts(new Date()),jstHour=Number(jstParts.find(x=>x.type==="hour")?.value??"99");
  if(jstHour>=NIGHT_PAUSE_START_HOUR&&jstHour<NIGHT_PAUSE_END_HOUR)return;
  const r=await runtime(),now=Date.now();
  const mu=Date.parse(r.maintenance_until||"");if(Number.isFinite(mu)&&mu>now)return;
  if(Array.isArray(r.maintenance_dates)&&r.maintenance_dates.includes(day))return;
  for(const w of Array.isArray(r.system_maintenance_windows)?r.system_maintenance_windows:[]){const a=Date.parse(w?.start||""),b=Date.parse(w?.end||"");if(Number.isFinite(a)&&Number.isFinite(b)&&a<=now&&now<b)return;}
  const ls=Date.parse(r.last_success_at||"");if(Number.isFinite(ls)&&now-ls<RECOVERY_MS)return;
  let st={};try{st=await read(RESTART_KEY,{})||{}}catch{}const before=JSON.stringify(st);
  try{
    const runs=await githubApi("GET","/actions/workflows/"+PRODUCTION_WORKFLOW+"/runs?per_page=20");
    if(!(runs.workflow_runs||[]).some(x=>x.status!=="completed")){
      await githubApi("POST","/actions/workflows/"+PRODUCTION_WORKFLOW+"/dispatches",{ref:"main"});
      st={...st,last_dispatch_at:nowIso(),last_dispatch_reason:Number.isFinite(ls)?"最終正常取得から"+Math.floor((now-ls)/60000)+"分":"最終正常取得なし",last_error:null};
    }
  }catch(err){st={...st,last_error:String(err).slice(0,500)};}
  await tokenExpiryNotice(st);
  if(JSON.stringify(st)!==before)await TOSS_KV.put(RESTART_KEY,JSON.stringify(st));
}
async function scheduledTask(){
  if(["2026-10-16","2026-10-17"].includes(today()))return;
  const now=new Date(),jstParts=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Tokyo",hour:"2-digit",hourCycle:"h23"}).formatToParts(now),jstHour=Number(jstParts.find(x=>x.type==="hour")?.value??"99");
  if(jstHour>=NIGHT_PAUSE_START_HOUR&&jstHour<NIGHT_PAUSE_END_HOUR)return;
  const c=await config();if(!c.monitor_enabled)return;let ls=await lineState(),r=await runtime();
  const scheduledStateBefore=JSON.stringify(ls);
  if(!ls.monitoring?.enabled)return;

  const currentDay=today();
  const windowState=refreshMonitoringWindow(ls,currentDay);
  if(windowState.ended){
    await notify(ls,["監視期間が終了しました。\n期間："+ymdw(windowState.previous_start)+" ～ "+ymdw(windowState.previous_end)],"監視終了通知失敗");
    ls.monitoring={enabled:false,start_date:null,end_date:null,enabled_at:null};
    ls.last_seen_state_saved_at=null;
    await setConfig(false);
    if(JSON.stringify(ls)!==scheduledStateBefore)await saveLine(ls);
    return;
  }

  const en=Date.parse(ls.monitoring.enabled_at||""),ru=Date.parse(r.updated_at||"");
  const er=r.errors&&typeof r.errors==="object"?r.errors:{};const payload={};for(const k of ERRORS)if(k!=="LINE送信エラー"&&er[k])payload[k]=er[k];const es=JSON.stringify(payload);
  const hasRuntimeErrors=Object.keys(payload).length>0;
  let hadRuntimeErrors=!!ls.last_toss_runtime_signature;
  if(hadRuntimeErrors){try{hadRuntimeErrors=Object.keys(JSON.parse(ls.last_toss_runtime_signature)).length>0}catch{hadRuntimeErrors=true}}
  // 起動中のruntimeには前回エラーが残る場合があるため、取得結果が確定するまで通知判定しない。
  // 予定メンテナンス中は、エラー状態を変化・通知させず、そのまま保持する。
  const runtimeReady=r.monitor_status!=="starting"&&r.monitor_status!=="running";
  if(r.monitor_status!=="maintenance"&&runtimeReady&&Number.isFinite(en)&&Number.isFinite(ru)&&ru>=en){
    if(hasRuntimeErrors){
      if(!hadRuntimeErrors)await notify(ls,["TOSS監視でエラーを検知しました。",es],"TOSSエラー通知失敗");
      // エラー内容が変化しても、継続中の同一障害として1回だけ通知する。
      ls.last_toss_runtime_signature=es;
    }else{
      if(hadRuntimeErrors)await notify(ls,["TOSS監視が正常状態に復旧しました。"],"復旧通知失敗");
      // 旧版で保存された空オブジェクト"{}"も、復旧通知せず静かに解除する。
      ls.last_toss_runtime_signature=null;
    }
  }

  let currentState=null;
  try{currentState=(await tossState()).state;}catch(err){await lineError("監視状態比較失敗: "+err);}
  if(currentState){
    const saved=String(currentState.saved_at||"");
    const st=dateOk(ls.monitoring.start_date)?ls.monitoring.start_date:null,ed=dateOk(ls.monitoring.end_date)?ls.monitoring.end_date:null;
    const savedEpoch=Date.parse(saved),todayStart=Date.parse(currentDay+"T07:00:00+09:00");
    const firstPostNightState=ls.night_baseline_date!==currentDay&&Number.isFinite(savedEpoch)&&savedEpoch>=todayStart;
    if(saved&&st&&ed){
      const shouldCompare=firstPostNightState||(!ls.last_seen_state_saved_at?true:(saved!==ls.last_seen_state_saved_at&&(!ls.monitoring.enabled_at||Date.parse(saved)>=Date.parse(ls.monitoring.enabled_at))));
      if(shouldCompare){
        let prev={statuses:{}};try{prev=await read(STATE_BAK_KEY,{statuses:{}})}catch{}
        const changes=[];
        for(const[k,v]of Object.entries(currentState.statuses||{})){if(!v||v.status!=="空き")continue;const p=k.split("|"),dd=p[0],d2=dateOk(dd)?dd:null;if(!d2||d2<st||d2>ed)continue;const old=prev.statuses?.[k];if(!old||old.status==="空き")continue;changes.push({date:dd,facility:v.facility||"不明",item:v.item||"不明",time_name:v.time_name||p[2],card_no:v.card_no==null?p[1]:v.card_no,from:old.status});}
        changes.sort((a,b)=>[a.date,hour(a.time_name)??99,Number(a.card_no)||9999,a.time_name].join("|").localeCompare([b.date,hour(b.time_name)??99,Number(b.card_no)||9999,b.time_name].join("|")));
        let delivered=true;
        if(changes.length){const sig=btoa(unescape(encodeURIComponent(JSON.stringify(changes))));if(sig!==ls.last_toss_alert_signature){delivered=await notify(ls,vacancyMessages(currentState,changes),"空き化通知失敗");if(delivered)ls.last_toss_alert_signature=sig;}}
        else ls.last_toss_alert_signature=null;
        // 送信に失敗した状態は比較済みにせず、次回の定期処理で同じ比較・通知をやり直す。
        if(delivered){
          ls.last_seen_state_saved_at=saved;
          if(firstPostNightState)ls.night_baseline_date=currentDay;
        }
      }
    }
  }

  if(r.monitor_status==="maintenance"){
    // 予定された停止は異常停止として扱わず、以前の停止疑い通知フラグも黙って解除する。
    ls.monitor_stale_notified=false;
  }else{
    // 起動/取得中は前夜の最終取得時刻を使わず、今回の実行開始時刻で停止を判定する。
    const cycleActive=r.monitor_status==="starting"||r.monitor_status==="running";
    const last=cycleActive?(r.process_started_at||r.last_scan_started_at||r.updated_at):(r.last_scan_started_at||r.updated_at);
    const ts=Date.parse(last||"");
    if(Number.isFinite(ts)&&(!Number.isFinite(en)||ts>=en)){
      const age=Date.now()-ts;
      if(age>STALE_MS&&!ls.monitor_stale_notified){
        await notify(ls,["TOSS監視停止の可能性があります。\n最終監視開始："+fresh(last)+"\n現在の状態から詳細を確認してください。"],"監視停止疑い通知失敗");
        ls.monitor_stale_notified=true;
      }else if(age<=STALE_MS&&ls.monitor_stale_notified){
        // 正常な再開そのものは通知しない。次回の停止判定に備えてフラグだけ解除する。
        ls.monitor_stale_notified=false;
      }
    }
  }
  if(JSON.stringify(ls)!==scheduledStateBefore)await saveLine(ls);
}
addEventListener("fetch",event=>event.respondWith((async()=>{const u=new URL(event.request.url);if(event.request.method==="GET"&&u.pathname==="/")return json({ok:true,service:"toss-line-bot"});if(event.request.method==="GET"&&u.pathname==="/calendar")return calendarPage();if(event.request.method==="GET"&&u.pathname==="/health")return json({ok:true,service:"toss-line-bot",kv_bound:typeof TOSS_KV!=="undefined",line_secret_configured:typeof LINE_CHANNEL_SECRET!=="undefined"&&!!LINE_CHANNEL_SECRET,line_access_token_configured:typeof LINE_CHANNEL_ACCESS_TOKEN!=="undefined"&&!!LINE_CHANNEL_ACCESS_TOKEN,allowed_user_configured:typeof LINE_ALLOWED_USER_ID!=="undefined"&&!!LINE_ALLOWED_USER_ID,github_dispatch_configured:typeof GITHUB_DISPATCH_TOKEN!=="undefined"&&!!GITHUB_DISPATCH_TOKEN});if(event.request.method!=="POST"||u.pathname!=="/callback")return new Response("Not Found",{status:404});const len=Number(event.request.headers.get("content-length")||0);if(len<=0||len>MAX_BODY)return new Response("Payload Too Large",{status:413});const body=await event.request.text();if(new TextEncoder().encode(body).length>MAX_BODY||!(await verify(body,event.request.headers.get("x-line-signature")||"")))return new Response("Bad Request",{status:400});let data;try{data=JSON.parse(body)}catch{return new Response("Bad Request",{status:400})}if(!Array.isArray(data.events))return new Response("Bad Request",{status:400});if(!data.events.length)return new Response("OK");if(["2026-10-16","2026-10-17"].includes(today()))return new Response("OK");event.waitUntil(webhook(data));return new Response("OK")})()));
// 片方が失敗してももう片方は最後まで実行し、失敗はこれまでどおりWorkerのエラーとして残す。
addEventListener("scheduled",event=>event.waitUntil(Promise.allSettled([scheduledTask(),restartTask()]).then(rs=>{const f=rs.find(x=>x.status==="rejected");if(f)throw f.reason;})));