import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
test('aggregate readiness and bounded shutdown when a helper ignores SIGTERM',async()=>{
 let healthy=false;
 const health=createServer((_,r)=>r.writeHead(healthy?200:503).end());health.listen(0,'127.0.0.1');await once(health,'listening');
 const reserve=createServer();reserve.listen(0,'127.0.0.1');await once(reserve,'listening');const port=reserve.address().port;await new Promise(r=>reserve.close(r));
 const child=spawn(process.execPath,[new URL('./supervise.mjs',import.meta.url).pathname],{env:{...process.env,ANVILKIT_SUPERVISOR_PORT:String(port),ANVILKIT_SUPERVISOR_SHUTDOWN_MS:'200',ANVILKIT_SUPERVISOR_PROCESSES:JSON.stringify([{prefix:'ANVILKIT_MCP_',health:`http://127.0.0.1:${health.address().port}/readyz`,command:[process.execPath,'-e',"process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"]}])},stdio:'ignore'});
 try{
  let response;for(let n=0;n<40;n++){response=await fetch(`http://127.0.0.1:${port}/readyz`).catch(()=>null);if(response)break;await sleep(25);}
  assert.equal(response.status,503);healthy=true;
  assert.equal((await fetch(`http://127.0.0.1:${port}/readyz`)).status,200);
  await sleep(100);const start=Date.now();const exit=once(child,'exit');child.kill('SIGTERM');assert.equal((await exit)[0],0);assert.ok(Date.now()-start<1000);
 } finally {child.kill('SIGKILL');health.closeAllConnections();health.close();}
});
