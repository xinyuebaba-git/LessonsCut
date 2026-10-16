import {useEffect,useRef,useState} from 'react';
import {ApiError,json,message} from './client';
import {roles,roleNames,type Configuration,type Asset} from './types';
import {Chip,Field,Glyph,Heading,Standards} from './ui';
type Entry={id:string;name:string;bytes?:number;file?:File;asset_id?:string};
export function Batch({config,assets,onStarted,onModels}:{config:Configuration;assets:Asset[];onStarted:()=>void;onModels:()=>void}){
 const [entries,setEntries]=useState<Entry[]>([]),[step,setStep]=useState(1),[name,setName]=useState('课程制作 · 新批次');
 const [intro,setIntro]=useState(config.template.intro_asset_id||''),[confirmedVersion,setConfirmedVersion]=useState(config.version);
 const [busy,setBusy]=useState(false),[progress,setProgress]=useState(''),[error,setError]=useState(''),[dragging,setDragging]=useState(false);
 const picker=useRef<HTMLInputElement>(null),uploaded=useRef(new Map<string,string>()),submission=useRef<{key:string;body:string}|null>(null);
 const [uncertain,setUncertain]=useState(false);
 const previousIntro=useRef(config.template.intro_asset_id);
 useEffect(()=>{const before=previousIntro.current;setIntro(value=>!value||value===before?config.template.intro_asset_id||'':value);previousIntro.current=config.template.intro_asset_id},[config.template.intro_asset_id]);
 function add(list:FileList|null){if(!list)return;const files=Array.from(list);if(files.some(f=>f.size>20*1024**3)){setError('单个视频不能超过 20 GiB。');return}if(files.some(f=>!f.type.startsWith('video/')&&!/\.(mp4|mov|mkv|avi|webm|m4v)$/i.test(f.name))){setError('请只选择视频文件。');return}
  setEntries(previous=>{const keys=new Set(previous.map(e=>e.id));const incoming=files.map(f=>({id:`${f.name}:${f.size}:${f.lastModified}`,name:f.name,bytes:f.size,file:f})).filter(f=>!keys.has(f.id));return [...previous,...incoming]});setError('')}
 function confirm(){if(!entries.length||entries.length>100){setError('每批支持 1 至 100 个课程视频。');return}setConfirmedVersion(config.version);setStep(2);setError('')}
 async function submit(){
  if(!entries.length||!intro||busy)return;
  setBusy(true);setError('');
  try{
   if(!submission.current){
    if(confirmedVersion!==config.version)throw new Error('模型配置已改变，请重新确认本批配置。');
    const ids:string[]=[];
    for(const [i,e] of entries.entries()){
     if(e.asset_id){ids.push(e.asset_id);continue}
     let id=uploaded.current.get(e.id);
     if(!id){setProgress(`正在上传 ${i+1} / ${entries.length}：${e.name}`);const form=new FormData();form.append('file',e.file!);const asset=await json<Asset>('/assets/uploads?kind=video',{method:'POST',body:form});id=asset.id;uploaded.current.set(e.id,id)}
     ids.push(id);
    }
    submission.current={key:crypto.randomUUID(),body:JSON.stringify({asset_ids:ids,intro_asset_id:intro,approval_policy:'exceptions_only',name:name.trim()||'课程制作',config_version:confirmedVersion})};
   }
   setProgress('确认整批任务入队…');setUncertain(true);
   const pending=submission.current;
   await json('/batches',{method:'POST',headers:{'Idempotency-Key':pending.key},body:pending.body});
   submission.current=null;uploaded.current.clear();setUncertain(false);setEntries([]);setStep(1);setProgress('');onStarted();
  }catch(e){
   if(e instanceof ApiError&&e.status>=400&&e.status<500){submission.current=null;setUncertain(false)}
   setError(message(e)+(submission.current?'。请重试确认同一批次，应用会使用原提交标识，避免重复入队。':'。已成功上传的文件会在重试时复用，尚未启动剪辑。'))
  }
  finally{setBusy(false)}
 }
 return <><Heading eyebrow="NEW BATCH" title="导入一批课程，统一制作。" description="每个视频单独识别课名、生成标题图，并保留完整连续授课。"><Chip>配置 v{config.version}</Chip></Heading>
 {error&&<p className="form-error" role="alert">{error}</p>}{progress&&<p className="toast" role="status">{progress}</p>}
 <fieldset disabled={busy||uncertain}><div className="stepper"><button className={step===1?'current':''} onClick={()=>setStep(1)}><b>01</b>选择课程<span>一次添加多个视频</span></button><div/><button className={step===2?'current':''} disabled={!entries.length} onClick={confirm}><b>02</b>确认并开始<span>核对本批规则与模型</span></button></div></fieldset>
 <div className="batch-layout"><div><section className="panel"><fieldset disabled={busy||uncertain}>{step===1?<><div className="panel-heading"><h2>本批课程</h2><span>{entries.length} / 100 个视频</span></div><input ref={picker} type="file" className="sr-only" tabIndex={-1} multiple accept="video/*,.mp4,.mov,.mkv,.avi,.webm,.m4v" aria-label="选择本批课程视频" onChange={e=>{add(e.target.files);e.target.value=''}}/>
 <button type="button" className={`drop-zone wide ${dragging?'dragging':''}`} onClick={()=>picker.current?.click()} onDragOver={e=>{e.preventDefault();if(!busy&&!uncertain)setDragging(true)}} onDragLeave={()=>setDragging(false)} onDrop={e=>{e.preventDefault();setDragging(false);if(!busy&&!uncertain)add(e.dataTransfer.files)}}><span className="upload-glyph"><Glyph name="upload"/></span><strong>选择或拖入一批课程视频</strong><p>支持多选，启动前可继续添加或移除。</p><span className="outline-button">选择视频</span><small>点击开始后才上传并处理 · 每个文件最多 20 GiB</small></button>
 <details className="library-picker"><summary>从已入库视频选择</summary>{assets.filter(a=>a.kind==='video'&&a.id!==intro).map(a=><label className="checkbox-line" key={a.id}><input type="checkbox" checked={entries.some(e=>e.asset_id===a.id)} onChange={e=>setEntries(p=>e.target.checked?[...p,{id:a.id,name:a.name,asset_id:a.id}]:p.filter(x=>x.asset_id!==a.id))}/>{a.name}</label>)}</details>
 {entries.length>0&&<div className="file-list"><div className="list-label"><span>待处理课程</span><button className="text-button" onClick={()=>setEntries([])}>清空清单</button></div>{entries.map((e,i)=><div className="file-row" key={e.id}><span className="file-number">{String(i+1).padStart(2,'0')}</span><div><strong>{e.name}</strong><small>{e.bytes===undefined?'已入库':`${(e.bytes/1024**2).toFixed(1)} MB`} · 标题待自动识别</small></div><button className="icon-button" aria-label={`移除 ${e.name}`} onClick={()=>setEntries(p=>p.filter(x=>x.id!==e.id))}><Glyph name="close"/></button></div>)}</div>}</>:<><div className="panel-heading"><h2>确认本批设置</h2><button className="text-button" onClick={()=>setStep(1)}>返回修改清单</button></div><Field label="批次名称"><input value={name} onChange={e=>setName(e.target.value)} maxLength={100}/></Field><Field label="本批已批准的统一片头" hint="默认来自剪辑模板；片头素材不加入课程任务。"><select value={intro} onChange={e=>setIntro(e.target.value)}><option value="">请选择已批准片头</option>{assets.filter(a=>a.kind==='video'&&!entries.some(e=>e.asset_id===a.id)).map(a=><option key={a.id} value={a.id}>{a.name}</option>)}</select></Field><div className="auto-title"><Chip tone="green">自动生成</Chip><h3>一条视频，一张课程标题页</h3><p>从画面与本地语音提取本课标题，只保留课名和已核实主讲人；沿用原风格生成 1.5 秒标题页并复核。</p><small>无原风格时{config.template.fallback_style?'采用统一课程模板':'转人工确认'}；需要概括课名时先人工确认。</small></div><div className="model-summary"><div className="section-label">本批模型 · 配置 v{confirmedVersion}<button className="text-button" onClick={onModels}>修改配置</button></div>{roles.map(role=><div key={role}><span>{roleNames[role]}</span><strong>{config.roles[role].model}</strong></div>)}</div>{confirmedVersion!==config.version&&<p className="inline-warning">配置已更新。<button className="text-button" onClick={()=>setConfirmedVersion(config.version)}>重新确认使用 v{config.version}</button></p>}<div className="disclosure"><strong>开始即授权本批处理</strong><p>本地分析接收视频与语音；监督服务接收必要抽帧和文字证据；生图服务接收本课文字与风格参考。模型调用会产生相应用量。</p></div><details><summary>查看 {entries.length} 个课程文件</summary>{entries.map(e=><p key={e.id}>{e.name}</p>)}</details></>}
 </fieldset><div className="panel-footer"><span>{step===1?'选择文件不会立即启动处理':`${entries.length} 个课程 · 异常才人工确认`}</span>{step===1?<button className="primary" disabled={busy||!entries.length||entries.length>100} onClick={confirm}>下一步：确认批次 <Glyph name="arrow"/></button>:<button className="primary" disabled={busy||!intro||(!uncertain&&confirmedVersion!==config.version)} onClick={()=>void submit()}>{busy?'提交中…':uncertain?'重试确认同一批次':'开始批量处理'}<Glyph name="arrow"/></button>}</div></section></div><aside className="batch-summary"><section className="panel"><span className="eyebrow">OUTPUT STANDARD</span><h2>统一的成片标准</h2><Standards/></section><div className="side-tip"><h3>标题图无需手动准备</h3><p>识别、生成与校验跟随每条课程自动进行，正常任务自动推进。</p><button className="text-button" onClick={onModels}>查看模型配置 →</button></div></aside></div></>
}
