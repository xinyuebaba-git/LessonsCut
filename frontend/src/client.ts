export class ApiError extends Error {constructor(public status:number,message:string){super(message)}}
export async function request(path:string,init:RequestInit={}):Promise<Response>{
 const headers=new Headers(init.headers);
 headers.set('X-LessonsCut-Request','1');
 if(init.body&&!(init.body instanceof FormData))headers.set('Content-Type','application/json');
 const response=await fetch(`/api/v1${path}`,{...init,headers,credentials:'same-origin'});
 if(!response.ok){
  let detail='请求失败';
  try{const data=await response.json();detail=typeof data.detail==='string'?data.detail:'请检查填写内容和数据格式'}catch{/* No raw server response in the UI. */}
  if(response.status===401&&path!='/auth/login')window.dispatchEvent(new Event('lessonscut:unauthorized'));
  throw new ApiError(response.status,response.status===401?'访问密码不正确或登录已过期':`${detail}（${response.status}）`);
 }
 return response;
}
export async function json<T>(path:string,init:RequestInit={}):Promise<T>{return (await request(path,init)).json()}
export async function download(path:string,filename:string){
 const response=await request(path);const url=URL.createObjectURL(await response.blob());
 const a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
}
export function message(error:unknown){return error instanceof Error?error.message:'操作失败，请重试'}
