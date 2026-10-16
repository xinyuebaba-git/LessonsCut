import {useEffect,useRef,type ReactNode,type KeyboardEvent} from 'react';
export function Mark(){return <span className="wordmark">Lessons<span>Cut</span><i>课程制作工作台</i></span>}
export function Glyph({name}:{name:'plus'|'batch'|'tasks'|'models'|'template'|'arrow'|'upload'|'close'}){
 const paths={plus:'M12 5v14M5 12h14',batch:'M4 4h7v7H4zM14 4h6v7h-6zM4 14h7v6H4zM14 14h6v6h-6z',tasks:'M8 6h12M8 12h12M8 18h12M3 6h.1M3 12h.1M3 18h.1',models:'M5 7h14M5 17h14M9 4v6M15 14v6',template:'M4 4h16v16H4zM4 9h16M10 9v11',arrow:'M5 12h14M14 7l5 5-5 5',upload:'M12 16V3M7 8l5-5 5 5M4 15v6h16v-6',close:'M6 6l12 12M18 6L6 18'};
 return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]}/></svg>
}
export function Field({label,children,hint}:{label:string;children:ReactNode;hint?:string}){return <label className="field"><span>{label}</span>{children}{hint&&<small>{hint}</small>}</label>}
export function Chip({children,tone='muted'}:{children:ReactNode;tone?:string}){return <span className={`chip ${tone}`}>{children}</span>}
export function Heading({eyebrow,title,description,children}:{eyebrow:string;title:string;description:string;children?:ReactNode}){return <div className="page-heading"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{children}</div>}
export function Standards(){return <><div className="mini-timeline"><div>片头</div><div>标题</div><div>连续授课</div></div>{[['片头','广州开放大学 · 6 秒'],['标题页','逐课自动生成 · 1.5 秒'],['授课内容','连续保留 · 原声'],['导出','1080p · MP4 · 新文件']].map(([k,v])=><div className="summary-row" key={k}><span>{k}</span><strong>{v}</strong></div>)}<div className="rule-note">只在标题推断、证据冲突或复核失败时转人工确认。</div></>}
export function Dialog({title,children,onClose}:{title:string;children:ReactNode;onClose:()=>void}){
 const ref=useRef<HTMLElement>(null);
 useEffect(()=>{const previous=document.activeElement as HTMLElement|null;ref.current?.focus();const overflow=document.body.style.overflow;document.body.style.overflow='hidden';return()=>{document.body.style.overflow=overflow;previous?.focus()}},[]);
 function keys(e:KeyboardEvent){
  if(e.key==='Escape'){e.preventDefault();onClose()}
  if(e.key!=='Tab')return;
  const nodes=ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]');
  if(!nodes?.length)return;const first=nodes[0],last=nodes[nodes.length-1];
  if(e.shiftKey&&(document.activeElement===first||document.activeElement===ref.current)){e.preventDefault();last.focus()}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}
 }
 return <div className="modal-backdrop" onClick={onClose}><section className="detail-dialog" ref={ref} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} onClick={e=>e.stopPropagation()} onKeyDown={keys}><div className="panel-heading"><h2>{title}</h2><button className="icon-button" aria-label="关闭详情" onClick={onClose}><Glyph name="close"/></button></div>{children}</section></div>
}
