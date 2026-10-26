import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";
const HEADERS = {"Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"content-type,x-sync-key","Access-Control-Allow-Methods":"GET,POST,OPTIONS","Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store"};
const DEFAULT_ACCOUNT = "hfm-5620";
const reply = (body,status=200) => new Response(JSON.stringify(body),{status,headers:HEADERS});
const number = (value) => {const n=Number(value);if(value==null||value===""||!Number.isFinite(n))throw new Error("Invalid amount");return n;};
const integer = (value) => {const n=number(value);if(!Number.isSafeInteger(n)||n<0)throw new Error("Invalid integer");return n;};
const stamp = (value) => {if(typeof value!=="string"||!/(Z|[+-]\d{2}:?\d{2})$/i.test(value)||!Number.isFinite(Date.parse(value)))throw new Error("Invalid timestamp");return value;};
const date = (value) => {if(typeof value!=="string"||!/^\d{4}-\d{2}-\d{2}$/.test(value)||new Date(value+"T00:00:00Z").toISOString().slice(0,10)!==value)throw new Error("Invalid date");return value;};
const rows = (value,limit) => {const xs=Array.isArray(value)?value:[value];if(xs.length>limit)throw new Error("Too many rows");return xs;};
function prepareUpload(body,account,auth,now=new Date().toISOString()) {
 if(!body||typeof body!=="object"||Array.isArray(body))throw new Error("Invalid payload");
 if(body.accountId!==undefined&&body.accountId!==account.id)throw new Error("Account mismatch");
 if(account.id!==DEFAULT_ACCOUNT&&!body.sourceAccount)throw new Error("Missing source account");
 if(body.sourceAccount&&(Number(body.sourceAccount.login)!==Number(auth.account_login)||body.sourceAccount.server!==auth.server||body.sourceAccount.currency!==account.currency))throw new Error("Source account mismatch");
 const result={};
 if(body.daily)result.daily=rows(body.daily,20000).map(r=>({account_id:account.id,trade_date:date(r.date),pnl:number(r.pnl),trades:integer(r.trades),wins:integer(r.wins),losses:integer(r.losses),source:"mt5",updated_at:now}));
 if(body.trades)result.trades=rows(body.trades,5000).map(r=>{
  if(r.symbol!==account.symbol||!["buy","sell"].includes(r.side))throw new Error("Invalid trade symbol or side");
  return {account_id:account.id,deal_ticket:integer(r.ticket),position_id:integer(r.positionId),trade_time:stamp(r.time),trade_date:date(r.date),symbol:account.symbol,side:r.side,volume:number(r.volume),price:number(r.price),profit:number(r.profit),swap:number(r.swap),commission:number(r.commission),fee:number(r.fee),net_pnl:number(r.netPnl),updated_at:now};
 });
 if(body.candles)result.candles=rows(body.candles,1000).map(r=>{
  if(r.symbol!==account.symbol||!["M1","M5","M15","H1"].includes(r.timeframe))throw new Error("Invalid candle symbol or timeframe");
  return {account_id:account.id,symbol:account.symbol,timeframe:r.timeframe,candle_time:stamp(r.time),open:number(r.open),high:number(r.high),low:number(r.low),close:number(r.close),tick_volume:integer(r.volume),updated_at:now};
 });
 if(body.status){const s=body.status;result.status={account_id:account.id,id:1,balance:number(s.balance),equity:number(s.equity),floating_pnl:number(s.floatingPnl),open_positions:integer(s.openPositions),updated_at:now};}
 return result;
}
async function readAccount(sb,account,tf) {
 const id=account.id;
 const [d,s,t,c]=await Promise.all([
  sb.from("daily_pnl").select("*").eq("account_id",id).order("trade_date"),
  sb.from("mt5_status").select("*").eq("account_id",id).eq("id",1).maybeSingle(),
  sb.from("mt5_trades").select("*").eq("account_id",id).order("trade_time",{ascending:false}).order("deal_ticket",{ascending:false}).limit(500),
  sb.from("xau_candles").select("*").eq("account_id",id).eq("symbol",account.symbol).eq("timeframe",tf).order("candle_time",{ascending:false}).limit(120)
 ]);
 if(d.error||s.error||t.error||c.error)throw new Error("Data read failed");
 return {daily:d.data||[],status:s.data,trades:t.data||[],candles:[...(c.data||[])].reverse()};
}
function combineAccounts(accounts,results,tf) {
 const days=new Map(),sum=(xs,key)=>xs.reduce((total,row)=>total+(Number(row&&row[key])||0),0);
 for(const result of results)for(const row of result.daily){
  const day=days.get(row.trade_date)||{account_id:"all",trade_date:row.trade_date,pnl:0,trades:0,wins:0,losses:0,source:"mt5",updated_at:row.updated_at};
  for(const key of ["pnl","trades","wins","losses"])day[key]+=Number(row[key])||0;
  if(Date.parse(row.updated_at)>Date.parse(day.updated_at))day.updated_at=row.updated_at;
  days.set(row.trade_date,day);
 }
 const activeIndexes=accounts.flatMap((a,i)=>a.is_active===false?[]:[i]);
 const activeAccounts=activeIndexes.map(i=>accounts[i]);
 const statuses=activeIndexes.map(i=>results[i].status),timestamps=statuses.map(s=>s&&s.updated_at);
 const complete=timestamps.length>0&&timestamps.every(t=>t&&Number.isFinite(Date.parse(t)));
 const oldest=complete?timestamps.reduce((a,b)=>Date.parse(a)<Date.parse(b)?a:b):null;
 const status={id:1,account_id:"all",balance:sum(statuses,"balance"),equity:sum(statuses,"equity"),floating_pnl:sum(statuses,"floating_pnl"),open_positions:sum(statuses,"open_positions"),updated_at:oldest,
  account_statuses:activeAccounts.map((a,i)=>({id:a.id,label:a.label,updated_at:timestamps[i]||null}))};
 const trades=results.flatMap(r=>r.trades).sort((a,b)=>Date.parse(b.trade_time)-Date.parse(a.trade_time)||Number(b.deal_ticket)-Number(a.deal_ticket)||String(a.account_id).localeCompare(String(b.account_id))).slice(0,500);
 // Price candles represent one gold market; prices are never added together.
 const chartIndex=results.findIndex((r,i)=>accounts[i].is_active!==false&&r.candles.length>0),chart=chartIndex<0?null:results[chartIndex];
 return {ok:true,accountId:"all",account:{id:"all",label:`HFM ${activeAccounts.length}口座合算`,currency:"JPY",symbol:chartIndex<0?"XAUUSD":accounts[chartIndex].symbol},accounts:activeAccounts,archivedAccounts:accounts.filter(a=>a.is_active===false),
  daily:[...days.values()].sort((a,b)=>a.trade_date.localeCompare(b.trade_date)),status,trades,candles:chart?chart.candles:[],chartAccountId:chartIndex<0?null:accounts[chartIndex].id,timeframe:tf};
}
async function handleRequest(req,sb) {
 if(req.method==="OPTIONS")return new Response("ok",{headers:HEADERS});
 try {
  if(req.method==="GET"){
   const url=new URL(req.url),accountId=url.searchParams.get("account")||DEFAULT_ACCOUNT,tf=url.searchParams.get("tf")||"M15";
   if(!["M1","M5","M15","H1"].includes(tf))return reply({ok:false,error:"Invalid timeframe"},400);
   const {data:accounts,error}=await sb.from("mt5_accounts").select("id,label,broker,suffix,currency,symbol,sort_order,is_active").order("sort_order");
   if(error)throw error;
   if(accountId==="all"){
    const results=await Promise.all(accounts.map(a=>readAccount(sb,a,tf)));
    return reply(combineAccounts(accounts,results,tf));
   }
   const account=accounts.find(a=>a.id===accountId);
   if(!account)return reply({ok:false,error:"Unknown account"},404);
   return reply({ok:true,accounts:accounts.filter(a=>a.is_active!==false),archivedAccounts:accounts.filter(a=>a.is_active===false),accountId,account,...await readAccount(sb,account,tf),timeframe:tf});
  }
  if(req.method==="POST"){
   const key=req.headers.get("x-sync-key")||"";
   if(!key)return reply({ok:false,error:"Unauthorized"},401);
   const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(key));
   const hash=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,"0")).join("");
   const {data:auth,error:authError}=await sb.from("mt5_account_auth").select("account_id,account_login,server").eq("key_hash",hash).maybeSingle();
   if(authError)throw authError;
   if(!auth)return reply({ok:false,error:"Unauthorized"},401);
   const {data:account,error:accountError}=await sb.from("mt5_accounts").select("id,currency,symbol,is_active").eq("id",auth.account_id).single();
   if(accountError)throw accountError;
   if(account.is_active===false)return reply({ok:false,error:"Account is archived"},403);
   let upload;
   try {upload=prepareUpload(await req.json(),account,auth);}catch(_){return reply({ok:false,error:"Invalid payload or account identity"},400);}
   // Validate every section first. Write status last so failed uploads do not appear fresh.
   for(const [key,table,conflict] of [["daily","daily_pnl","account_id,trade_date"],["trades","mt5_trades","account_id,deal_ticket"],["candles","xau_candles","account_id,symbol,timeframe,candle_time"],["status","mt5_status","account_id,id"]]){
    if(upload[key]&&(!Array.isArray(upload[key])||upload[key].length)){
     const {error}=await sb.from(table).upsert(upload[key],{onConflict:conflict});if(error)throw error;
    }
   }
   return reply({ok:true,accountId:account.id});
  }
  return reply({ok:false,error:"Method not allowed"},405);
 }catch(_){return reply({ok:false,error:"Sync service unavailable"},500);}
}
Deno.serve(req=>handleRequest(req,createClient(Deno.env.get("SUPABASE_URL"),Deno.env.get("SUPABASE_SERVICE_ROLE_KEY"))));
