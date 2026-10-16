import {useCallback,useEffect,useState,type FormEvent} from 'react';
import {createRoot} from 'react-dom/client';
import {ApiError,json,message,request} from './client';
import {Batch} from './Batch';
import {Models,Template} from './Models';
import {Tasks} from './Tasks';
import {Mark,Glyph,Field} from './ui';
import type {Asset,Configuration,Job} from './types';
import './preview.css';
import './workbench.css';

type Page='batch'|'tasks'|'models'|'template';
function Login({onLogin}:{onLogin:()=>void}){
 const [password,setPassword]=useState(''),[visible,setVisible]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
 async function login(e:FormEvent){e.preventDefault();setBusy(true);setError('');try{await json('/auth/login',{method:'POST',body:JSON.stringify({password})});setPassword('');onLogin()}catch(err){setError(message(err))}finally{setBusy(false)}}
 return <div className="login-shell"><div className="prototype-banner"><span>自建服务器 · 课程制作工作台</span><a href="/preview.html">查看交互原型 ↗</a></div><div className="login-grid"><section className="login-story"><Mark/><div><span className="eyebrow">LESSONSCUT / 课程制作</span><h1>让每一节课，<br/>都有完整的表达。</h1><p>从一批原始视频，到统一片头、准确标题与连续授课。把时间留给教学。</p><div className="login-timeline"><span>6s 统一片头</span><span>1.5s 课程标题</span><span>连续授课与原声</span></div></div><small>本地识别 · 独立监督 · 逐课生图</small></section><section className="login-form"><div className="login-box"><h2>登录 LessonsCut</h2><p>使用部署管理员设置的访问密码。</p>{error&&<p className="form-error" role="alert">{error}</p>}<form onSubmit={e=>void login(e)}><fieldset disabled={busy}><Field label="访问密码" hint="这是本应用的访问密码，与模型 API Key 无关。"><div className="password-wrap"><input type={visible?'text':'password'} value={password} aria-label="访问密码" autoComplete="current-password" onChange={e=>setPassword(e.target.value)} required/><button className="text-button" type="button" onClick={()=>setVisible(!visible)}>{visible?'隐藏':'显示'}</button></div></Field><button className="primary wide" disabled={busy||!password} type="submit">{busy?'正在登录…':'登录工作台'}<Glyph name="arrow"/></button></fieldset></form><details><summary>访问密码是什么？</summary><p>对应服务器配置中的 LESSONSCUT_ADMIN_TOKEN，用于保护视频、任务和模型配置。忘记时联系部署管理员。</p></details></div></section></div></div>
}
function App(){
 const [authenticated,setAuthenticated]=useState<boolean|null>(null),[page,setPage]=useState<Page>('batch'),[assets,setAssets]=useState<Asset[]>([]),[jobs,setJobs]=useState<Job[]>([]),[config,setConfig]=useState<Configuration|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const refresh=useCallback(async(signal?:AbortSignal)=>{const [a,j,c]=await Promise.all([json<Asset[]>('/assets',{signal}),json<Job[]>('/jobs',{signal}),json<Configuration>('/config',{signal})]);setAssets(a);setJobs(j);setConfig(previous=>!previous||c.version>=previous.version?c:previous)},[]);
 useEffect(()=>{sessionStorage.removeItem('lessonscut-token');const controller=new AbortController();json('/auth/session',{signal:controller.signal}).then(()=>setAuthenticated(true)).catch(e=>{if(controller.signal.aborted)return;setAuthenticated(false);if(!(e instanceof ApiError&&e.status===401))setError(message(e))});const expired=()=>setAuthenticated(false);window.addEventListener('lessonscut:unauthorized',expired);return()=>{controller.abort();window.removeEventListener('lessonscut:unauthorized',expired)}},[]);
 useEffect(()=>{if(!authenticated)return;let active=true;let timer:ReturnType<typeof setTimeout>;const controller=new AbortController();async function poll(){try{await refresh(controller.signal);if(active)setError('')}catch(e){if(active&&!(e instanceof ApiError&&e.status===401))setError(message(e))}finally{if(active)timer=setTimeout(()=>void poll(),5000)}}void poll();return()=>{active=false;clearTimeout(timer);controller.abort()}},[authenticated,refresh]);
 async function reload(){try{await refresh()}catch(e){setError(message(e))}}
 async function logout(){try{await request('/auth/logout',{method:'POST'});setAuthenticated(false);setConfig(null);setAssets([]);setJobs([]);setNotice('')}catch(e){setError(message(e))}}
 function navigate(p:Page){setPage(p);setNotice('')}
 function saved(c:Configuration){setConfig(c);void reload()}
 if(authenticated===null)return <div className="loading-screen"><Mark/><p>正在连接工作台…</p></div>;
 if(!authenticated)return <><Login onLogin={()=>{setAuthenticated(true);setError('');setPage('batch')}}/>{error&&<p role="alert" className="form-error">{error}</p>}</>;
 if(!config)return <div className="loading-screen"><Mark/><p>正在载入课程和模型配置…</p>{error&&<p role="alert">{error}</p>}<button className="text-button" onClick={()=>void logout()}>退出登录</button></div>;
 const pending=jobs.filter(j=>j.state==='NEEDS_REVIEW').length;
 return <div className="app-shell"><aside className="sidebar"><Mark/><div className="workspace-label">制作工作区</div><nav aria-label="主导航">{([['batch','新建批次'],['tasks','处理任务'],['models','模型配置'],['template','剪辑模板']] as [Page,string][]).map(([id,label])=><button key={id} className={page===id?'active':''} aria-current={page===id?'page':undefined} onClick={()=>navigate(id)}><Glyph name={id}/>{label}{id==='tasks'&&pending>0&&<span className="nav-count">{pending}</span>}</button>)}</nav><div className="sidebar-bottom"><div className="server-label">自建服务器</div><small>模型接口按角色配置<br/>配置版本 v{config.version}</small><button className="text-button" onClick={()=>void logout()}>退出登录 ↗</button></div></aside><div className="workspace"><header className="topbar"><span>课程制作 <span className="breadcrumb">/</span>{{batch:'新建批次',tasks:'处理任务',models:'模型配置',template:'剪辑模板'}[page]}</span><span className="workspace-user">管理员<button className="text-button mobile-logout" onClick={()=>void logout()}>退出</button></span></header><main>{error&&<p className="form-error" role="alert">{error}</p>}{notice&&<div className="toast" role="status"><span>{notice}</span><button onClick={()=>setNotice('')} aria-label="关闭提示"><Glyph name="close"/></button></div>}
 <div hidden={page!=='batch'}><Batch config={config} assets={assets} onStarted={()=>{setPage('tasks');setNotice('整批任务已入队，Worker 将按队列逐条处理。');void reload()}} onModels={()=>navigate('models')}/></div>
 <div hidden={page!=='tasks'}><Tasks jobs={jobs} assets={assets} onNew={()=>navigate('batch')} onChanged={()=>void reload()}/></div>
 <div hidden={page!=='models'}><Models config={config} onSaved={saved} notify={setNotice}/></div>
 <div hidden={page!=='template'}><Template config={config} assets={assets} onSaved={saved} notify={setNotice}/></div>
 </main><footer>LessonsCut · 课程制作工作台<span>逐课自动标题 · 异常人工确认 · 原文件不覆盖</span></footer></div></div>
}
createRoot(document.getElementById('root')!).render(<App/>);
