// Small public test application. Inputs and identities are synthetic fixtures.
const http = require('node:http');
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync('app.sqlite');
db.exec(`CREATE TABLE IF NOT EXISTS accounts(username TEXT PRIMARY KEY,email TEXT,password TEXT);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,username TEXT);
  CREATE TABLE IF NOT EXISTS workbooks(name TEXT PRIMARY KEY);
  CREATE TABLE IF NOT EXISTS filter_views(name TEXT PRIMARY KEY);
  CREATE TABLE IF NOT EXISTS reactions(username TEXT PRIMARY KEY);
  CREATE TABLE IF NOT EXISTS seeds(version INTEGER PRIMARY KEY);
  INSERT OR IGNORE INTO accounts VALUES('alice','alice@example.test','fixture-password');
  INSERT OR IGNORE INTO accounts VALUES('bob','bob@example.test','fixture-password');`);
if (!db.prepare('SELECT version FROM seeds WHERE version=1').get()) {
  db.exec("BEGIN; INSERT INTO workbooks VALUES('Public workbook'); INSERT INTO filter_views VALUES('Active'); INSERT INTO reactions VALUES('inherited-reader'); INSERT INTO seeds VALUES(1); COMMIT");
}
const failures = process.env.ARC_FIXTURE_FAILURES === '1';
const html = username => `<!doctype html><html><body><main>
  <h1>Fixture collaboration</h1><a href="/signin">Sign in</a>
  <a href="/sheet">Public workbook</a><a href="/issue">Public issue</a>
  ${username ? `<p>${username}</p>` : ''}
  <form id="login"><label>Username or email<input id="username"></label>
  <label>Password<input id="password" type="password"></label><button>Sign in</button></form>
  <p id="message"></p></main><script>
  ${failures ? "fetch('/api/fault')" : ''}
  document.getElementById('login').onsubmit=async event=>{event.preventDefault();
    const response=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:document.getElementById('username').value,
      password:document.getElementById('password').value})});
    const result=await response.json();document.getElementById('message').textContent=result.username||'Invalid credentials';};
  </script></body></html>`;
const sheet = `<!doctype html><html><body><main><h1>Public workbook</h1>
  <div role="grid" aria-label="Worksheet grid" style="display:grid;grid-template-columns:60px 60px">
  ${['A1','B1','A2','B2'].map(cell=>`<div role="gridcell" aria-label="${cell}" aria-selected="false" style="height:35px">${cell}</div>`).join('')}</div>
  <button id="data">Data</button><div id="menu" role="menu" hidden>
  ${['Create filter','Clear filter','Save filter view','Filter views'].map(name=>`<button role="menuitem">${name}</button>`).join('')}</div>
  <div id="dialog"></div></main><script>
  let active=false; const host=document.getElementById('dialog');
  document.querySelectorAll('[role=gridcell]').forEach(cell=>cell.onmousedown=()=>
    document.querySelectorAll('[role=gridcell]').forEach(c=>c.setAttribute('aria-selected','true')));
  document.getElementById('data').onclick=()=>document.getElementById('menu').hidden=false;
  for(const item of document.querySelectorAll('[role=menuitem]')) item.onclick=async()=>{
    document.getElementById('menu').hidden=true;
    if(item.textContent==='Create filter') active=true;
    if(item.textContent==='Clear filter') active=false;
    if(item.textContent==='Save filter view'){
      host.innerHTML='<div role="dialog" aria-label="Save filter view"><label>Filter view name<input id="view"></label><button id="save">Save view</button><button id="close">Close</button><p role="alert"></p></div>';
      document.getElementById('close').onclick=()=>host.innerHTML='';
      document.getElementById('save').onclick=async()=>{
        const response=await fetch('/views',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('view').value,active})});
        host.querySelector('[role=alert]').textContent=(await response.json()).error;
      };
    }
    if(item.textContent==='Filter views'){
      const names=await (await fetch('/views')).json();host.innerHTML='<div role="dialog" aria-label="Filter views"></div>';
      const dialog=host.firstChild;
      for(const name of names){const button=document.createElement('button');button.textContent=name;
        button.onclick=()=>{const remove=document.createElement('button');remove.textContent='Delete filter view';
          remove.onclick=async()=>{await fetch('/views/'+encodeURIComponent(name),{method:'DELETE'});button.remove();remove.remove();};dialog.append(remove);};dialog.append(button);}
    }
  };
  </script></body></html>`;
function issue(username){
  const total=db.prepare('SELECT count(*) AS total FROM reactions').get().total;
  const owned=username&&db.prepare('SELECT username FROM reactions WHERE username=?').get(username);
  return `<!doctype html><html><body><main><h1>Public issue</h1><p id="total">${total} ${total===1?'reaction':'reactions'}</p>
    <button id="add" ${username?'':'disabled'}>Add reaction</button><div id="menu" role="menu" hidden><button role="menuitem">+1</button></div>
    <div id="remove">${owned?'<button>Remove +1 reaction</button>':''}</div></main><script>
    const render=result=>{document.getElementById('total').textContent=result.total+' '+(result.total===1?'reaction':'reactions');
      document.getElementById('remove').innerHTML=result.owned?'<button>Remove +1 reaction</button>':'';
      const remove=document.querySelector('#remove button');if(remove)remove.onclick=()=>change('remove');};
    const change=async action=>render(await(await fetch('/reaction',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})})).json());
    document.getElementById('add').onclick=()=>document.getElementById('menu').hidden=false;
    document.querySelector('[role=menuitem]').onclick=()=>{document.getElementById('menu').hidden=true;change('add');};
    const remove=document.querySelector('#remove button');if(remove)remove.onclick=()=>change('remove');
    </script></body></html>`;
}
const server = http.createServer(async (request, response) => {
  const id=(request.headers.cookie||'').match(/session=([^;]+)/)?.[1];
  const session=id&&db.prepare('SELECT username FROM sessions WHERE id=?').get(id);
  if (request.url === '/api/health') return response.end('ok');
  if (request.url === '/api/fault') {response.statusCode=500; return response.end('fixture failure');}
  if (request.url === '/login') {
    let body=''; for await (const chunk of request) body+=chunk;
    const input=JSON.parse(body);
    const account=db.prepare('SELECT * FROM accounts WHERE (username=? OR email=?) AND password=?')
      .get(input.username,input.username,input.password);
    response.setHeader('Content-Type','application/json');
    if (!account) return response.end(JSON.stringify({error:'Invalid credentials'}));
    const id='session-'+account.username;
    db.prepare('INSERT OR IGNORE INTO sessions VALUES(?,?)').run(id,account.username);
    response.setHeader('Set-Cookie',`session=${id}; Path=/`);
    return response.end(JSON.stringify({username:failures&&account.username==='bob'?'unexpected':account.username}));
  }
  if(request.url==='/views'&&request.method==='GET'){
    response.setHeader('Content-Type','application/json');return response.end(JSON.stringify(db.prepare('SELECT name FROM filter_views').all().map(row=>row.name)));
  }
  if(request.url==='/views'&&request.method==='POST'){
    let body='';for await(const chunk of request)body+=chunk;const input=JSON.parse(body);
    const duplicate=db.prepare('SELECT name FROM filter_views WHERE name=?').get(input.name.trim());
    const error=duplicate?'Filter view name already exists':!input.active?'Create a filter first':'';
    response.setHeader('Content-Type','application/json');return response.end(JSON.stringify({error}));
  }
  if(request.url.startsWith('/views/')&&request.method==='DELETE'){
    db.prepare('DELETE FROM filter_views WHERE name=?').run(decodeURIComponent(request.url.slice(7)));return response.end('deleted');
  }
  if(request.url==='/reaction'&&request.method==='POST'){
    let body='';for await(const chunk of request)body+=chunk;const input=JSON.parse(body);
    if(!session){response.statusCode=403;return response.end('sign in');}
    if(input.action==='add')db.prepare('INSERT OR IGNORE INTO reactions VALUES(?)').run(session.username);
    else db.prepare('DELETE FROM reactions WHERE username=?').run(session.username);
    response.setHeader('Content-Type','application/json');return response.end(JSON.stringify({total:db.prepare('SELECT count(*) AS total FROM reactions').get().total,owned:input.action==='add'}));
  }
  response.setHeader('Content-Type','text/html');
  response.end(request.url==='/sheet'?sheet:request.url==='/issue'?issue(session?.username):html(session?.username));
});
server.listen(Number(process.env.PORT || 0),'127.0.0.1');
process.on('SIGTERM',()=>server.close(()=>{db.close();process.exit(0);}));
