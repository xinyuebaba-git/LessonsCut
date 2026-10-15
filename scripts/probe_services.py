"""Explicit live development probes. Uses only a synthetic image, never course files."""
import argparse,json
from pathlib import Path
from PIL import Image,ImageDraw
from sqlalchemy import select
from lessonscut.config import get_settings
from lessonscut.store import Store,Call,uid
from lessonscut.providers import Providers


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--qwen',action='store_true')
    parser.add_argument('--supervisor',action='store_true')
    parser.add_argument('--image',action='store_true',help='One billable image generation request')
    args=parser.parse_args()
    cfg=get_settings();db=Store(cfg.database_url);jid='probe-'+uid()
    root=cfg.data_dir/'work'/jid;root.mkdir(parents=True)
    image=root/'synthetic.png';im=Image.new('RGB',(320,180),'white');ImageDraw.Draw(im).text((20,60),'LessonsCut 2026',fill='black');im.save(image)
    p=Providers(cfg,db,jid,1)
    for enabled,role in [(args.qwen,'recognizer'),(args.supervisor,'supervisor')]:
        if enabled:
            result=p.chat(role,'识别测试图中的文字，只返回 {"text":"实际文字"}。',[image])
            print(role,json.dumps(result,ensure_ascii=False))
    if args.image:
        path=p.generate_title('LessonsCut 接口测试',None,'白色背景，绿色简洁无衬线文字，居中',root/'generated.png')
        print('image_generated',path)
    with db.session() as s:
        print('calls',json.dumps([{'role':c.role,'model':c.model,'status':c.status,**json.loads(c.details)} for c in s.scalars(select(Call).where(Call.job_id==jid))],ensure_ascii=False))


if __name__=='__main__':main()
