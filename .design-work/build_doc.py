from pathlib import Path
import re, json, hashlib
from docx import Document
from docx.shared import Mm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / 'AVCL形式化验证语言与执行系统详细设计_v0.1.docx'
FONT = 'C:/Windows/Fonts/msyh.ttc'

def diagram(name, state=False):
    im = Image.new('RGB', (1600, 740 if not state else 660), 'white')
    dr = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 32)
    fs = ImageFont.truetype(FONT, 26)
    def box(x, y, w, h, lines, fill='#F1F4F7'):
        dr.rounded_rectangle((x,y,x+w,y+h), radius=10, fill=fill, outline='#697786', width=3)
        for j,line in enumerate(lines):
            bb=dr.textbbox((0,0),line,font=f)
            dr.text((x+(w-bb[2])/2,y+(h-len(lines)*44)/2+j*44),line,font=f,fill='#14202B')
    def arrow(points):
        dr.line(points,fill='#455868',width=4)
        x,y=points[-1]; px,py=points[-2]
        import math
        a=math.atan2(y-py,x-px)
        dr.polygon([(x,y),(x-17*math.cos(a-.45),y-17*math.sin(a-.45)),(x-17*math.cos(a+.45),y-17*math.sin(a+.45))],fill='#455868')
    if not state:
        dr.text((35,15),'候选生成与外部效果之间的受控路径',font=ImageFont.truetype(FONT,36),fill='black')
        box(35,110,265,125,['Agent 与模型','不可信候选'], '#FFFFFF')
        box(410,110,310,125,['验证与规范化','结构和证据'])
        box(830,110,310,125,['授权与执行网关','预留和派发准入'])
        box(1250,110,310,125,['隔离适配器','实际外部请求'])
        arrow([(300,172),(410,172)])
        arrow([(720,172),(830,172)])
        arrow([(1140,172),(1250,172)])
        box(410,355,310,125,['政策包与注册表','版本和可信假设'])
        box(830,355,310,125,['共享持久账本','操作 预算 事件'])
        box(1250,355,310,125,['核对监督器','未决结果与义务'])
        arrow([(565,355),(565,235)])
        arrow([(720,418),(830,418)])
        arrow([(985,235),(985,355)])
        arrow([(1250,418),(1140,418)])
        arrow([(1405,355),(1405,235)])
        dr.line((30,555,1560,555),fill='#D9D9D9',width=2)
        for i,t in enumerate(['模型不能直接签发许可或持有工具凭据。','授权事务与远端生效分开；未知结果由账本和监督器持续管理。','控制面发布不可变政策包，执行面只加载经过核验的对应版本。']):
            dr.text((40,580+i*45),t,font=fs,fill='#333333')
    else:
        box(30,110,255,110,['Prepared','已预留'])
        box(395,110,285,110,['Dispatched','本地已准入'])
        box(800,110,280,110,['Unknown','结果未知'])
        box(1200,30,355,110,['Confirmed','业务效果已确认'])
        box(1200,240,355,110,['RejectedNoEffect','终局无业务效果'])
        box(1200,450,355,110,['PartialEffect','已观察到部分效果'])
        box(30,405,380,110,['AbortedBeforeDispatch','准入前安全撤销'])
        arrow([(285,165),(395,165)])
        arrow([(680,165),(800,165)])
        arrow([(1080,165),(1125,165),(1125,85),(1200,85)])
        arrow([(1080,165),(1125,165),(1125,295),(1200,295)])
        arrow([(1080,165),(1125,165),(1125,505),(1200,505)])
        arrow([(155,220),(155,405)])
        arrow([(680,110),(725,110),(725,10),(1380,10),(1380,30)])
        dr.text((795,255),'超时不释放预留',font=fs,fill='black')
        dr.text((435,365),'可信回执可直接进入结果状态',font=fs,fill='#444444')
        dr.text((435,410),'核对不确定时维持原状态',font=fs,fill='#444444')
        dr.text((35,585),'费用结算独立于业务状态；只有终局费用证据才能释放对应剩余预留。',font=fs,fill='black')
    path=ROOT / name
    im.save(path)
    return path

def set_font(style, name, size, east='Microsoft YaHei'):
    style.font.name=name; style.font.size=Pt(size); style.font.color.rgb=RGBColor(0,0,0)
    rf=style.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in list(rf.attrib):
        if attr.endswith('Theme'): del rf.attrib[attr]
    for k,v in [('ascii',name),('hAnsi',name),('eastAsia',east)]:rf.set(qn('w:'+k),v)

doc=Document()
# The bundled blank template carries decorative title borders; remove them.
for border in list(doc.styles.element.xpath('.//w:pBdr')):
    border.getparent().remove(border)
sec=doc.sections[0]
sec.page_width=Mm(210);sec.page_height=Mm(297)
sec.top_margin=Mm(20);sec.bottom_margin=Mm(19)
sec.left_margin=Mm(22);sec.right_margin=Mm(22)
sec.header_distance=Mm(9);sec.footer_distance=Mm(9)
set_font(doc.styles['Normal'],'Calibri',10.5)
normal=doc.styles['Normal'].paragraph_format
normal.line_spacing=1.18; normal.space_after=Pt(6)
normal.widow_control=True
for name,size in [('Title',24),('Subtitle',11),('Heading 1',16),('Heading 2',12)]:
    set_font(doc.styles[name],'Calibri',size)
    pf=doc.styles[name].paragraph_format
    pf.space_before=Pt(14 if name=='Heading 1' else 9)
    pf.space_after=Pt(7)
    pf.keep_with_next=True
    if name.startswith('Heading'):doc.styles[name].font.bold=True
    doc.styles[name].font.italic=False
    doc.styles[name].font.underline=False
doc.styles['Title'].font.bold=False
for sname in ['Code','Table Body','Table Header','Caption']:
    if sname not in doc.styles:
        doc.styles.add_style(sname,1)
set_font(doc.styles['Code'],'Consolas',8.5)
cp=doc.styles['Code'].paragraph_format
cp.line_spacing=1.05;cp.space_before=Pt(3);cp.space_after=Pt(6)
cp.left_indent=Mm(3);cp.right_indent=Mm(1)
cp.keep_together=True
set_font(doc.styles['Table Body'],'Calibri',9)
set_font(doc.styles['Table Header'],'Calibri',9)
doc.styles['Table Header'].font.bold=True
doc.styles['Table Header'].font.color.rgb=RGBColor(255,255,255)
for name in ['Table Body','Table Header']:
    pf=doc.styles[name].paragraph_format
    pf.line_spacing=1.12;pf.space_before=Pt(3);pf.space_after=Pt(3)
set_font(doc.styles['Caption'],'Calibri',9)
doc.styles['Caption'].paragraph_format.space_after=Pt(7)
footer=sec.footer.paragraphs[0]
footer.alignment=WD_ALIGN_PARAGRAPH.CENTER
r=footer.add_run('AVCL 详细设计 0.1   ·   ');r.font.size=Pt(8)
field=OxmlElement('w:fldSimple');field.set(qn('w:instr'),'PAGE');footer._p.append(field)
doc.core_properties.title='AVCL 形式化验证语言与执行系统详细设计'
doc.core_properties.subject='核心语言 编译验证 事务协议 故障恢复 证明与验收规格'
doc.core_properties.author=''
doc.core_properties.keywords='AVCL,Agent,形式化验证,详细设计'
doc.core_properties.comments=''

def table(rows):
    n=len(rows[0]); t=doc.add_table(rows=0,cols=n)
    t.alignment=WD_TABLE_ALIGNMENT.CENTER;t.autofit=False
    if n==4: widths=[23,63,38,42]
    elif n==3: widths=[36,65,65]
    else: widths=[166/n]*n
    # Fit narrower identifier fields for the more descriptive matrices.
    if rows[0][0] in ['编号','用例']: widths=[16,71,39,40] if n==4 else [16,70,80]
    if rows[0][0]=='表':widths=[33,77,56]
    if rows[0][0]=='接口':widths=[69,47,50]
    if rows[0][0]=='步骤':widths=[48,32,32,54]
    if rows[0][0]=='错误码':widths=[65,22,79]
    for c,w in zip(t.columns,widths):c.width=Mm(w)
    pr=t._tbl.tblPr
    borders=OxmlElement('w:tblBorders')
    for edge in ['top','left','bottom','right','insideH','insideV']:
        el=OxmlElement('w:'+edge);el.set(qn('w:val'),'single');el.set(qn('w:sz'),'4');el.set(qn('w:color'),'D9D9D9');borders.append(el)
    pr.append(borders)
    margins=OxmlElement('w:tblCellMar')
    for edge,val in [('top',65),('bottom',65),('left',85),('right',85)]:
        el=OxmlElement('w:'+edge);el.set(qn('w:w'),str(val));el.set(qn('w:type'),'dxa');margins.append(el)
    pr.append(margins)
    for i,row in enumerate(rows):
        cells=t.add_row().cells
        trPr=t.rows[-1]._tr.get_or_add_trPr()
        cant=OxmlElement('w:cantSplit');trPr.append(cant)
        if i==0:
            rep=OxmlElement('w:tblHeader');trPr.append(rep)
        for j,(c,txt) in enumerate(zip(cells,row)):
            c.width=Mm(widths[j]); c.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
            p=c.paragraphs[0];p.style=doc.styles['Table Header' if i==0 else 'Table Body']
            if i==0: p.paragraph_format.keep_with_next=True
            p.add_run(txt)
            if j==0 and rows[0][0] in ['编号','用例']:p.alignment=WD_ALIGN_PARAGRAPH.CENTER
            sh=OxmlElement('w:shd');sh.set(qn('w:fill'),'243E53' if i==0 else ('F2F5F7' if i%2==0 else 'FFFFFF'));c._tc.get_or_add_tcPr().append(sh)
    p=doc.add_paragraph();p.paragraph_format.space_after=Pt(2);p.paragraph_format.space_before=Pt(0);p.paragraph_format.line_spacing=0.4

arch=diagram('architecture.png')
state=diagram('states.png',True)
lines=(ROOT/'design.md').read_text(encoding='utf8').splitlines()
i=0;fig=0
while i<len(lines):
    line=lines[i].strip()
    if not line:i+=1;continue
    if line.startswith('```'):
        i+=1;block=[]
        while i<len(lines) and not lines[i].startswith('```'):
            block.append(lines[i]);i+=1
        p=doc.add_paragraph('\n'.join(block),'Code')
        # Let long code blocks continue across pages; preserve each visual line.
        p.paragraph_format.widow_control=True
    elif line.startswith('|'):
        rows=[]
        while i<len(lines) and lines[i].strip().startswith('|'):
            rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')]);i+=1
        table(rows);continue
    elif line.startswith('# '):doc.add_paragraph(line[2:],'Title')
    elif line.startswith('### '):doc.add_paragraph(line[4:],'Heading 2')
    elif line.startswith('## '):
        if line.startswith('## 1 '):doc.add_page_break()
        doc.add_paragraph(line[3:],'Heading 1')
    elif line in ['!ARCH!','!STATE!']:
        p=doc.add_paragraph();p.paragraph_format.keep_with_next=True
        pic=p.add_run().add_picture(str(arch if line=='!ARCH!' else state),width=Mm(166))
        pic._inline.docPr.set('descr','受控执行组件及数据流' if line=='!ARCH!' else '业务执行状态与结算分离')
        fig+=1;doc.add_paragraph('图 '+str(fig)+'  '+('执行系统组件与信任边界' if line=='!ARCH!' else '外部操作的主要状态路径'),'Caption')
    else:
        p=doc.add_paragraph(line)
        if line.startswith('版本 '):p.style=doc.styles['Subtitle']
    i+=1
doc.save(OUT)
print(json.dumps({'output':str(OUT),'bytes':OUT.stat().st_size,'paragraphs':len(doc.paragraphs),'tables':len(doc.tables)},ensure_ascii=False))
