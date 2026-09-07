from pm2_animation_lab.paths import is_data_directory
"""Static-frame diagnostic viewer; timed playback belongs to the pipeline."""
import argparse
import json
from pathlib import Path
import numpy as np
from PIL import Image
from pm2_animation_lab.pm2_activity_scene_profiles import PROFILES
from pm2_animation_lab.pm2_activity_video_index import confined, file_hash, write_json

NAMES=dict(zip([f'TRG{i:03d}' for i in range(10)],['自然科学','诗文','神学','军事','剑术','格斗术','魔法','礼仪','绘画','舞蹈']))
NAMES.update(zip([f'JOB{i:03d}' for i in range(15)],['家庭管理','育幼院','旅馆','农场','教堂','餐馆','伐木场','美容院','工地','狩猎区','墓园','家庭教师','酒店','酒家','大夜总会']))


def build(output,candidate_root,report_roots,root,candidate_overrides=()):
    root=Path(root).resolve();output=confined(output,root)
    if not is_data_directory(root):raise ValueError('External data directory required')
    output.mkdir(parents=True,exist_ok=False);(output/'diff').mkdir()
    reports={};bindings=[]
    for r in report_roots:
        for p in sorted(confined(r,root).glob('*.json')):
            d=json.loads(p.read_text(encoding='utf-8'))
            if 'runs' in d and 'candidate_manifests' in d:
                reports[d['scene']]=d;bindings.append({'path':str(p),'sha256':file_hash(p)})
    data=[]
    for scene in sorted(PROFILES,key=lambda s:(not s.startswith('TRG'),s)):
        item={'scene':scene,'name':NAMES[scene],'raw':{},'report':reports.get(scene)}
        for branch in ['success','failure','mischief','sunday']:
            p=confined(candidate_root,root)/scene/branch/'candidate.json'
            for override in candidate_overrides:
                alternate=confined(override,root)/scene/branch/'candidate.json'
                if alternate.is_file():p=alternate
            d=json.loads(p.read_text(encoding='utf-8'));item['raw'][branch]=[str((p.parent/f['image']).resolve().as_uri()) for f in d['frames']]
        if item['report']:
            report=item['report']
            memo={}
            for run in report['runs']:
                if run['different_pixels']:
                    key=(run['actual_image'],run['candidate_image'])
                    if key not in memo:
                        a=np.array(Image.open(key[0]).convert('RGB'));b=np.array(Image.open(key[1]).convert('RGB'))
                        diff=np.zeros_like(a);diff[np.any(a!=b,axis=2)]=[255,68,90]
                        p=output/'diff'/f'{scene}_{len(memo):03d}.png';Image.fromarray(diff).save(p);memo[key]=p.as_uri()
                    run['difference_image']=memo[key]
                for k in ['actual_image','candidate_image','reconstructed_image']:
                    if k in run:run[k]=Path(run[k]).as_uri()
        data.append(item)
    html=HTML.replace('__DATA__',json.dumps(data,ensure_ascii=True).replace('<','\\u003c'))
    (output/'index.html').write_text(html,encoding='utf-8')
    write_json(output/'manifest.json',{'reports':bindings,'viewer_script_sha256':file_hash(__file__),
        'scenes':len(data),'native_reports':len(reports),
        'claim':'Untimed still-frame diagnostics; no playback, publication receipt or actual RNG claim'})
    print(output/'index.html')


HTML=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PM2 课程与打工 · 静帧诊断</title><style>
*{box-sizing:border-box}body{margin:0;background:#11161c;color:#e5eaf1;font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1450px;margin:auto;padding:28px}h1{font-size:27px;margin:0}p{color:#a9b5c3;margin:8px 0 18px}select,button{background:#25303d;color:#eaf0f7;border:1px solid #506073;border-radius:6px;padding:9px 12px;font:inherit}button{cursor:pointer}button:hover{background:#35475a}.bar{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.panel{background:#1b242e;padding:14px;border-radius:10px}h2{font-size:16px;margin:0 0 10px}canvas{width:100%;background:#06090d;image-rendering:pixelated;aspect-ratio:2.5}#seek{width:100%}#stats{padding:15px;border-left:3px solid #70c2bc;background:#1b242e}.note{color:#edc586}.small{font-size:13px}a{color:#94d5ff}table{border-collapse:collapse;width:100%;margin-top:24px}td,th{text-align:left;border-bottom:1px solid #33404e;padding:9px}tr:hover{background:#1b242e}summary{cursor:pointer;font-weight:600;margin-top:24px}@media(max-width:850px){.grid{grid-template-columns:1fr}main{padding:16px}}
</style><main><h1>课程与打工动画 · 静帧诊断</h1><p>10 门课程 / 15 项打工 · 原生 320 × 128 活动窗口 · 本页仅用于选帧分析；动画播放须通过统一入口复核。</p>
<div class="bar"><select id="scene"></select><select id="mode"><option value="fit">按实录对齐查看像素</option><option value="raw">人工输入的原始合成序列</option></select><select id="branch"><option value="success">成功条件</option><option value="failure">失败条件</option><option value="mischief">怠工条件</option><option value="sunday">星期日条件</option></select><label><input id="copy" type="checkbox" checked>加入刷新复制重建</label></div>
<p id="warning" class="note"></p><div class="grid"><div class="panel"><h2>游戏实录</h2><canvas id="native" width="320" height="128"></canvas></div><div class="panel"><h2 id="candidateTitle">源码候选 / 观测拟合</h2><canvas id="candidate" width="320" height="128"></canvas></div><div class="panel"><h2>差异像素（红色）</h2><canvas id="diff" width="320" height="128"></canvas></div></div>
<div class="bar"><button id="prev">上一帧</button><button id="next">下一帧</button><label>源码 tick <input id="rawtick" type="number" min="0" value="0" step="1"></label><span id="position"></span></div><input id="seek" type="range" min="0" max="10000" value="0" step="1"><div id="stats"></div><p id="details" class="small"></p>
<details open><summary>25 项覆盖与差异清单</summary><p class="small">“像素解释”包含无序候选检索与按观测拟合的刷新复制，只用于定位差异。它不证明动作顺序、实际随机数、未观察分支或硬件计时预测。</p><table><thead><tr><th>项目</th><th>实录帧</th><th>完整画面检索</th><th>刷新解释</th><th>仍有差异</th></tr></thead><tbody id="rows"></tbody></table></details></main>
<script>const DATA=__DATA__;const $=s=>document.getElementById(s);let current=0,t=0,runIndex=0;const cache=new Map();
function draw(id,url){const c=$(id),ctx=c.getContext('2d');ctx.fillStyle='#06090d';ctx.fillRect(0,0,320,128);if(!url)return;let im=cache.get(url);if(!im){im=new Image();cache.set(url,im);im.onload=()=>render();im.src=url}if(im.complete&&im.naturalWidth)ctx.drawImage(im,0,0,320,128)}
DATA.forEach((s,i)=>{const o=document.createElement('option');o.value=i;o.textContent=s.scene+' '+s.name;$('scene').append(o);const tr=document.createElement('tr'),r=s.report;let vals=[s.scene+' '+s.name,r?r.frame_count:'未建立对照',r?r.exact_retrieval_frames:'—',r?r.source_copy_explained_frames:'—',r?r.unexplained_frames:'—'];vals.forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.append(td)});tr.onclick=()=>{$('scene').value=i;select()};$('rows').append(tr)});
function select(){current=+$('scene').value;t=0;$('rawtick').value=0;$('seek').max=DATA[current].report?.duration_ms||0;render()}
function render(){const s=DATA[current],r=s.report,fit=$('mode').value==='fit';$('branch').disabled=fit;$('copy').disabled=!fit;$('rawtick').disabled=fit;$('seek').disabled=!fit||!r;
 $('warning').textContent=fit?'本模式按实录挑选可解释的源码静帧，并可拟合刷新复制位置；仅供像素诊断，不是已证明顺序的重放。':'本模式选择人工输入条件的源码 tick，没有分配播放时长，也不保证与实录条件相同。';
 let run=null;if(r){runIndex=Math.max(0,r.runs.findIndex(x=>t>=x.start_ms&&t<x.end_ms));if(t>=r.duration_ms)runIndex=r.runs.length-1;run=r.runs[runIndex]}
 const raw=s.raw[$('branch').value],tick=Math.max(0,Math.min(raw.length-1,Math.floor(+$('rawtick').value)||0));$('rawtick').max=raw.length-1;$('rawtick').value=tick;
 draw('native',fit?run?.actual_image:null);let fitted=fit&&$('copy').checked&&run?.reconstructed_image;draw('candidate',fit?(fitted||run?.candidate_image):raw[tick]);draw('diff',fit&&!fitted?run?.difference_image:null);
 $('position').textContent=fit?'实录位置 '+(t/1000).toFixed(3)+' 秒':'源码 tick '+tick+'（未校时）';$('seek').value=t;
 $('stats').textContent=r?`本片段 ${r.frame_count} 个原生帧；完整画面检索 ${r.exact_retrieval_frames} 帧；刷新复制解释 ${r.source_copy_explained_frames} 帧；仍有差异 ${r.unexplained_frames} 帧。`:'该项目尚无可用的实际对照报告。';
 $('details').replaceChildren();if(fit&&run)$('details').append(document.createTextNode(`原片帧 ${run.first_frame}–${run.end_frame_exclusive-1}；候选条件 ${run.candidate_branch}，tick ${run.candidate_tick}；当前不同像素 ${fitted?0:run.different_pixels} / ${run.pixel_count}。`)); }
function step(d){if($('mode').value==='raw')$('rawtick').value=+$('rawtick').value+d;else{const r=DATA[current].report;if(r)t=r.runs[Math.max(0,Math.min(r.runs.length-1,runIndex+d))].start_ms}render()}
$('scene').onchange=select;$('mode').onchange=render;$('branch').onchange=render;$('copy').onchange=render;$('rawtick').oninput=render;$('seek').oninput=()=>{t=+$('seek').value;render()};$('prev').onclick=()=>step(-1);$('next').onclick=()=>step(1);select();
</script></html>'''

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--candidate-root',required=True)
    p.add_argument('--candidate-override',action='append',default=[])
    p.add_argument('--report-root',action='append',default=[]);p.add_argument('--quarantine',required=True)
    a=p.parse_args();build(a.output,a.candidate_root,a.report_root,a.quarantine,a.candidate_override)
