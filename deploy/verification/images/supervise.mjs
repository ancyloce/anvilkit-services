// One owning-service container, separately supervised trusted processes.
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
const specs=JSON.parse(process.env.ANVILKIT_SUPERVISOR_PROCESSES);
const timeout=Number(process.env.ANVILKIT_SUPERVISOR_SHUTDOWN_MS ?? 20000);
const port=Number(process.env.ANVILKIT_SUPERVISOR_PORT ?? 9199);
if(!Array.isArray(specs)||!specs.length||!Number.isFinite(timeout)||timeout<=0) throw Error('invalid supervisor configuration');
let stopping=false;
const children=[];
function stop(code) {
 if(stopping)return;
 stopping=true;
 const kill=setTimeout(()=>{for(const c of children){try{process.kill(-c.pid,'SIGKILL');}catch{}} process.exit(code);},timeout);
 for(const c of children){try{process.kill(-c.pid,'SIGTERM');}catch{}}
 Promise.all(children.map(c=>c.exitCode!==null||c.signalCode!==null?Promise.resolve():new Promise(r=>c.once('exit',r)))).then(()=>{
  clearTimeout(kill);server.closeAllConnections();server.close(()=>process.exit(code));
 });
}
const server=createServer(async(req,res)=>{
 if(req.url==='/healthz')return res.writeHead(stopping?503:200).end();
 if(req.url!=='/readyz')return res.writeHead(404).end();
 const status=await Promise.all(specs.map(async s=>{try{return (await fetch(s.health,{signal:AbortSignal.timeout(750)})).ok;}catch{return false;}}));
 res.writeHead(!stopping&&status.every(Boolean)?200:503).end();
});
server.listen(port,'0.0.0.0');
server.on('error',()=>stop(1));
for(const spec of specs){
 const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>!k.startsWith('ANVILKIT_')||k.startsWith(spec.prefix)));
 const child=spawn(spec.command[0],spec.command.slice(1),{env,stdio:'inherit',detached:true,cwd:spec.cwd});
 children.push(child);
 child.on('error',()=>stop(1));
 child.on('exit',()=>{if(!stopping)stop(1);});
}
process.on('SIGTERM',()=>stop(0));
process.on('SIGINT',()=>stop(0));
